"""Private GitHub setup over temporary Git repos and a fake native gh runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import setup_github as helper

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="Git is required for local repository fixtures")


def git(repo, *arguments):
    result = subprocess.run([GIT, *arguments], cwd=repo, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    assert result.returncode == 0, "Temporary Git fixture command failed"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    config = tmp_path / "empty-gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_TEMPLATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "專案"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    git(root, "add", "source.py")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Initial fixture")
    return root


class FakeRunner:
    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        self.authenticated = True
        self.login_name = "owner"
        self.exists = False
        self.private = True
        self.permission = "ADMIN"
        self.empty = True
        self.archived = False
        self.view_error = False
        self.name = "owner/project"
        self.push_failure = None
        self.remote_branch = True

    @staticmethod
    def result(argv, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def __call__(self, argv, *, cwd, env, timeout, capture=True):
        self.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout, "capture": capture,
                           "terminal_prompt": env.get("GIT_TERMINAL_PROMPT"), "host": env.get("GH_HOST")})
        args = argv[1:]
        if argv[0] == "fake-gh.exe":
            if args == ["api", "user", "--jq", ".login"]:
                return self.result(argv, 0, self.login_name + "\n") if self.authenticated else self.result(argv, 4, stderr="Authentication required")
            if args[:2] == ["auth", "login"]:
                self.authenticated = True
                return self.result(argv)
            if args[:2] == ["repo", "view"]:
                if self.view_error:
                    return self.result(argv, 1, stderr="Network unavailable")
                if not self.exists:
                    return self.result(argv, 1, stderr="GraphQL: Could not resolve to a Repository with the name")
                return self.result(argv, stdout=json.dumps({"nameWithOwner": self.name, "isPrivate": self.private,
                    "viewerPermission": self.permission, "isEmpty": self.empty, "isArchived": self.archived,
                    "url": "https://github.com/" + self.name}))
            if args[:2] == ["repo", "create"]:
                assert "--private" in args and "--push" not in args
                assert args[args.index("--source") + 1] == str(self.repo.resolve())
                self.exists = True
                git(self.repo, "remote", "add", "origin", "https://github.com/owner/project.git")
                return self.result(argv)
            raise AssertionError("Unexpected fake GitHub command")
        if args[:2] == ["ls-remote", "--heads"]:
            return self.result(argv, stdout=(git(self.repo, "rev-parse", "HEAD") + "\trefs/heads/main\n") if self.remote_branch else "")
        if args[:1] == ["push"]:
            assert "--force" not in args and "--mirror" not in args
            assert env["GIT_TERMINAL_PROMPT"] == "0" and timeout == 90
            if self.push_failure == "timeout":
                raise subprocess.TimeoutExpired(argv, timeout)
            if self.push_failure == "rejected":
                return self.result(argv, 1, stderr="[rejected] main -> main (non-fast-forward)")
            if self.push_failure:
                return self.result(argv, 1, stderr="Network unavailable")
            self.empty = False
            return self.result(argv, stdout="Done\n")
        return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", check=False)

    def gh_calls(self, command):
        return [item for item in self.calls if item["argv"][0] == "fake-gh.exe" and item["argv"][1:3] == command]

    def pushes(self):
        return [item for item in self.calls if item["argv"][1:2] == ["push"]]


def setup(repo):
    runner = FakeRunner(repo)
    return helper.GitHubSetup(repo, runner=runner, gh="fake-gh.exe", git=GIT), runner


def test_new_private_repository_is_verified_before_normal_push_and_upstream_is_saved(repo):
    selected, runner = setup(repo)
    original_head = git(repo, "rev-parse", "HEAD")
    result = selected.connect("owner/project")
    assert result["ok"] and result["created"] and result["pushed"]
    assert result["url"] == "https://github.com/owner/project"
    assert git(repo, "config", "--local", "branch.main.remote") == "origin"
    assert git(repo, "config", "--local", "branch.main.merge") == "refs/heads/main"
    assert git(repo, "rev-parse", "HEAD") == original_head
    create = runner.gh_calls(["repo", "create"])[0]
    assert "--private" in create["argv"] and "--push" not in create["argv"]
    push = runner.pushes()[0]
    assert push["argv"][1:] == ["push", "--porcelain", "origin", "HEAD:refs/heads/main"]
    assert runner.calls.index(push) > max(runner.calls.index(item) for item in runner.gh_calls(["repo", "view"]))


def test_connect_is_idempotent_for_verified_private_origin(repo):
    selected, runner = setup(repo)
    assert selected.connect("owner/project")["pushed"]
    again = selected.connect("owner/project")
    assert again["pushed"] and not again["created"]
    assert len(runner.gh_calls(["repo", "create"])) == 1
    assert any(item["argv"][1:3] == ["ls-remote", "--heads"] for item in runner.calls)


def test_missing_authentication_never_creates_or_connects_remote(repo):
    selected, runner = setup(repo)
    runner.authenticated = False
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "github_login_required"
    assert not runner.gh_calls(["repo", "create"]) and not runner.pushes()
    assert git(repo, "remote") == ""


def test_owner_must_match_authenticated_personal_account(repo):
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError) as error:
        selected.connect("another-owner/project")
    assert error.value.code == "github_owner_mismatch"
    assert not runner.gh_calls(["repo", "create"]) and not runner.pushes()


@pytest.mark.parametrize("name", ["https://github.com/owner/project", "owner/project.git", "owner/project/extra",
                                  "-option/project", "owner/..", "token@github.com:owner/project"])
def test_repository_argument_cannot_contain_urls_credentials_or_options(repo, name):
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError):
        selected.connect(name)
    assert runner.calls == []


@pytest.mark.parametrize("origin", ["https://github.com/another/project.git", "https://example.com/owner/project.git",
    "https://synthetic-user:synthetic-password@github.com/owner/project.git", "https://[invalid/owner/project"])
def test_conflicting_or_credentialed_origin_is_never_overwritten(repo, origin):
    git(repo, "remote", "add", "origin", origin)
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError):
        selected.connect("owner/project")
    assert git(repo, "remote", "get-url", "origin") == origin
    assert not runner.gh_calls(["repo", "create"]) and not runner.pushes()
    public = json.dumps(selected.status())
    assert "synthetic-password" not in public


def test_distinct_push_destination_is_rejected(repo):
    git(repo, "remote", "add", "origin", "https://github.com/owner/project.git")
    git(repo, "config", "--local", "remote.origin.pushurl", "https://github.com/another/project.git")
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "unsafe_origin" and not runner.pushes()


def test_exact_root_rejects_parent_repository_lookup_and_linked_worktree(repo, tmp_path):
    nested = repo / "nested"
    nested.mkdir()
    selected, runner = setup(nested)
    with pytest.raises(helper.SetupError):
        selected.connect("owner/project")
    assert not runner.calls
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "side", str(linked))
    selected, runner = setup(linked)
    with pytest.raises(helper.SetupError):
        selected.connect("owner/project")
    assert not runner.calls


def test_bare_and_uncommitted_repositories_are_not_connectable(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    git(bare, "init", "--bare")
    selected, runner = setup(bare)
    with pytest.raises(helper.SetupError):
        selected.connect("owner/project")
    assert not runner.calls
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-b", "main")
    selected, runner = setup(empty)
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "local_commit_required" and not runner.gh_calls(["repo", "create"])


def test_detached_head_cannot_be_pushed_by_helper(repo):
    git(repo, "checkout", "--detach")
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "local_commit_required" and not runner.pushes()


def test_existing_nonempty_unrelated_repository_is_not_adopted(repo):
    selected, runner = setup(repo)
    runner.exists, runner.empty = True, False
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "github_repository_not_empty"
    assert git(repo, "remote") == "" and not runner.pushes()


def test_existing_empty_private_repository_can_be_connected_without_recreation(repo):
    selected, runner = setup(repo)
    runner.exists = True
    result = selected.connect("owner/project")
    assert result["ok"] and not result["created"] and result["pushed"]
    assert not runner.gh_calls(["repo", "create"])


@pytest.mark.parametrize("field,value", [("private", False), ("permission", "READ"), ("archived", True), ("name", "another/project")])
def test_private_writable_repository_metadata_is_mandatory(repo, field, value):
    selected, runner = setup(repo)
    runner.exists = True
    setattr(runner, field, value)
    with pytest.raises(helper.SetupError):
        selected.connect("owner/project")
    assert git(repo, "remote") == "" and not runner.pushes()


def test_view_network_failure_is_not_mistaken_for_a_missing_repository(repo):
    selected, runner = setup(repo)
    runner.view_error = True
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "github_repository_unavailable" and not runner.gh_calls(["repo", "create"])


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_push_failure_retains_local_commits_and_upstream_for_safe_retry(repo, failure):
    selected, runner = setup(repo)
    before = git(repo, "rev-parse", "HEAD")
    runner.push_failure = failure
    result = selected.connect("owner/project")
    assert not result["ok"] and result["push_pending"] and result["retryable"]
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "config", "--local", "branch.main.remote") == "origin"
    assert git(repo, "config", "--local", "branch.main.merge") == "refs/heads/main"
    runner.push_failure = None
    assert selected.connect("owner/project")["pushed"]


def test_non_fast_forward_is_never_forced_or_reset(repo):
    selected, runner = setup(repo)
    runner.push_failure = "rejected"
    result = selected.connect("owner/project")
    assert result["code"] == "push_rejected" and not result["retryable"]
    assert not any(item["argv"][1:2] in (["reset"], ["pull"], ["fetch"]) for item in runner.calls)
    assert all("--force" not in item["argv"] for item in runner.calls)


def test_nonempty_remote_without_current_branch_is_not_given_new_upstream(repo):
    git(repo, "remote", "add", "origin", "https://github.com/owner/project.git")
    selected, runner = setup(repo)
    runner.exists, runner.empty, runner.remote_branch = True, False, False
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "remote_branch_missing" and not runner.pushes()
    assert selected._config("branch.main.remote") is None


def test_existing_different_branch_upstream_is_not_replaced(repo):
    git(repo, "config", "--local", "branch.main.remote", "elsewhere")
    selected, runner = setup(repo)
    with pytest.raises(helper.SetupError) as error:
        selected.connect("owner/project")
    assert error.value.code == "upstream_conflict"
    assert git(repo, "config", "--local", "branch.main.remote") == "elsewhere"
    assert not runner.gh_calls(["repo", "create"])


def test_status_uses_only_login_api_and_native_login_inherits_terminal(repo):
    selected, runner = setup(repo)
    runner.authenticated = False
    status = selected.status()
    assert status["git"]["valid"] and status["github"]["authenticated"] is False
    assert status["github"]["login"] is None
    assert not any("token" in item["argv"] or item["argv"][1:3] == ["auth", "status"] for item in runner.calls)
    assert selected.login()["login"] == "owner"
    native = runner.gh_calls(["auth", "login"])[0]
    assert native["capture"] is False and native["host"] == "github.com"
    assert native["argv"][1:] == ["auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"]


def test_gh_discovery_uses_newest_local_native_version(tmp_path, monkeypatch):
    monkeypatch.setattr(helper.shutil, "which", lambda name: None)
    for version in ("2.99.0", "2.103.2"):
        binary = tmp_path / ".tools" / f"gh-{version}" / "bin" / "gh.exe"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture executable, never run")
    assert "2.103.2" in helper.discover_gh(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows wrapper validation")
@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1"])
def test_windows_shell_wrappers_are_not_executed(repo, suffix):
    selected = helper.GitHubSetup(repo, gh="gh" + suffix, git="git" + suffix, runner=lambda *args, **kwargs: pytest.fail("Wrapper executed"))
    assert selected.gh is None and selected.git is None
    assert not selected.status()["git"]["valid"]


def test_repo_option_works_before_or_after_subcommand(repo):
    assert helper.parse_args(["--repo", str(repo), "status"]).repo == repo
    assert helper.parse_args(["connect", "--repository", "owner/project", "--repo", str(repo)]).sub_repo == repo
