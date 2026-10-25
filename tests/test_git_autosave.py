"""Real temporary Git repositories; pushes are always intercepted, never networked."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tools.git_autosave import (
    AutosaveError, DEFAULT_CONFIG, GitAutosave, contains_secret, load_config,
    main, trusted_github_url,
)


def git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["git", "-C", str(repo), *args], shell=False, capture_output=True,
                            env=env, timeout=15, check=True)
    return result.stdout.decode("utf-8").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "專案 space"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Autosave Test")
    git(root, "config", "user.email", "autosave-test@example.invalid")
    (root / "main.py").write_text("print('initial')\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n*.log\n", encoding="utf-8")
    git(root, "add", "main.py", ".gitignore")
    git(root, "commit", "-m", "initial")
    return root


def configured(repo: Path, *, clock=None, push=False) -> GitAutosave:
    watcher = GitAutosave(repo, **({"clock": clock} if clock else {}))
    value = watcher.init_config()
    value["enabled"] = True
    value["push"] = push
    watcher.config_path.write_text(json.dumps(value), encoding="utf-8")
    return watcher


def test_bridge_capability_records_are_never_automatic_git_candidates(repo):
    watcher = configured(repo)
    (repo / "office-bridge.json").write_text('{"token":"private fixture capability"}', encoding="utf-8")
    result = watcher.tick(force=True)
    assert result["status"] == "blocked"
    assert result["skipped_files"][0]["path"] == "office-bridge.json"
    assert git(repo, "log", "-1", "--format=%s") == "initial"


def test_snapshot_synchronizes_head_and_normal_index(repo: Path) -> None:
    watcher = configured(repo)
    before = git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "新程式.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    result = watcher.tick(force=True)
    assert result["status"] == "committed", result
    assert result["commit"] != before
    assert git(repo, "show", "HEAD:src/新程式.ts") == "export const answer = 42;"
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "diff", "--cached") == ""
    assert not (repo / ".git" / "index.lock").exists()
    assert watcher.tick(force=True)["status"] == "clean"


def test_tracked_delete_and_rename_are_saved(repo: Path) -> None:
    watcher = configured(repo)
    (repo / "main.py").rename(repo / "renamed.py")
    result = watcher.tick(force=True)
    assert result["status"] == "committed", result
    assert git(repo, "ls-tree", "--name-only", "HEAD") == ".gitignore\nrenamed.py"
    assert git(repo, "status", "--porcelain") == ""


def test_debounce_resets_when_contents_change(repo: Path) -> None:
    now = [100.0]
    watcher = configured(repo, clock=lambda: now[0])
    (repo / "main.py").write_text("one\n")
    assert watcher.tick()["status"] == "debouncing"
    now[0] += 7
    (repo / "main.py").write_text("two\n")
    assert watcher.tick()["status"] == "debouncing"
    now[0] += 7.9
    assert watcher.tick()["status"] == "debouncing"
    now[0] += .2
    assert watcher.tick()["status"] == "committed"


def test_existing_staged_work_is_preserved_byte_for_byte(repo: Path) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("staged\n")
    git(repo, "add", "main.py")
    (repo / "main.py").write_text("unstaged\n")
    old_head = git(repo, "rev-parse", "HEAD")
    index = (repo / ".git" / "index").read_bytes()
    result = watcher.tick(force=True)
    assert result["status"] == "blocked"
    assert "User-staged" in result["reason"]
    assert git(repo, "rev-parse", "HEAD") == old_head
    assert (repo / ".git" / "index").read_bytes() == index
    assert git(repo, "show", ":main.py") == "staged"
    assert (repo / "main.py").read_text() == "unstaged\n"


def test_preexisting_index_lock_is_never_removed(repo: Path) -> None:
    watcher = configured(repo)
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"another git owns this")
    (repo / "main.py").write_text("change\n")
    result = watcher.tick(force=True)
    assert result["status"] == "busy"
    assert lock.read_bytes() == b"another git owns this"


def test_lock_race_after_initial_check_does_not_unlink_foreign_lock(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("change\n")
    original = watcher._candidates
    lock = repo / ".git" / "index.lock"
    def race():
        result = original()
        lock.write_bytes(b"concurrent git")
        return result
    monkeypatch.setattr(watcher, "_candidates", race)
    assert watcher.tick(force=True)["status"] == "error"
    assert lock.read_bytes() == b"concurrent git"


def test_stage_race_is_rechecked_after_lock(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("change\n")
    original = watcher._candidates
    def stage_between_checks():
        result = original()
        git(repo, "add", "main.py")
        return result
    monkeypatch.setattr(watcher, "_candidates", stage_between_checks)
    result = watcher.tick(force=True)
    assert result["status"] == "error"
    assert "User-staged" in result["reason"]
    assert git(repo, "show", ":main.py") == "change"
    assert not (repo / ".git" / "index.lock").exists()


def test_cas_failure_preserves_original_index_and_head(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("change\n")
    old = git(repo, "rev-parse", "HEAD")
    index = (repo / ".git" / "index").read_bytes()
    original = watcher._run
    def reject_update(args, **kwargs):
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(args, 1, b"", b"not printed")
        return original(args, **kwargs)
    monkeypatch.setattr(watcher, "_run", reject_update)
    assert watcher.tick(force=True)["status"] == "error"
    assert git(repo, "rev-parse", "HEAD") == old
    assert (repo / ".git" / "index").read_bytes() == index
    assert not (repo / ".git" / "index.lock").exists()


def test_worktree_edit_after_snapshot_stays_unstaged(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("snapshot\n")
    original = watcher._run
    def edit_after_snapshot(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "commit-tree":
            (repo / "main.py").write_text("newer edit\n")
        return result
    monkeypatch.setattr(watcher, "_run", edit_after_snapshot)
    assert watcher.tick(force=True)["status"] == "committed"
    assert git(repo, "show", "HEAD:main.py") == "snapshot"
    assert git(repo, "diff", "--cached") == ""
    assert git(repo, "status", "--porcelain") == "M main.py"
    assert (repo / "main.py").read_text() == "newer edit\n"


@pytest.mark.parametrize("marker", ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"])
def test_in_progress_operation_pauses(repo: Path, marker: str) -> None:
    watcher = configured(repo)
    (repo / ".git" / marker).touch()
    (repo / "main.py").write_text("change\n")
    assert watcher.tick(force=True)["status"] == "busy"


def test_ignored_private_and_unapproved_files_remain_untracked(repo: Path) -> None:
    watcher = configured(repo)
    (repo / "ignored").mkdir()
    (repo / "ignored" / "private.py").write_text("ignored\n")
    (repo / ".env").write_text("SECRET=never_publish\n")
    (repo / "credentials.json").write_text('{"private":true}')
    (repo / "archive.zip").write_bytes(b"not source")
    (repo / "src").mkdir()
    (repo / "src" / "good.sql").write_text("select 1;\n")
    result = watcher.tick(force=True)
    assert result["status"] == "committed", result
    assert result["files"] == ["src/good.sql"]
    names = git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".env" not in names and "credentials.json" not in names
    assert "archive.zip" not in names and "ignored/private.py" not in names


def test_ignored_tracked_change_is_not_staged(repo: Path) -> None:
    watcher = configured(repo)
    (repo / ".gitignore").write_text("main.py\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore tracked file")
    (repo / "main.py").write_text("do not snapshot\n")
    result = watcher.tick(force=True)
    assert result["status"] == "blocked"
    assert result["skipped_files"][0]["reason"] == "Ignored by Git."
    assert git(repo, "show", "HEAD:main.py") == "print('initial')"


def test_secret_file_is_skipped_without_echoing_credential(repo: Path) -> None:
    watcher = configured(repo)
    token = "ghp_" + "Ab9" * 15
    (repo / "bad.py").write_text('access_token = "' + token + '"\n')
    (repo / "good.py").write_text("answer = 42\n")
    result = watcher.tick(force=True)
    assert result["status"] == "committed"
    assert result["files"] == ["good.py"]
    assert token not in json.dumps(result)
    assert "bad.py" not in git(repo, "ls-tree", "--name-only", "HEAD")


def test_actual_staged_blobs_are_revalidated_for_racing_secret(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("safe\n")
    original = watcher._run
    def inject_before_add(args, **kwargs):
        if args[0] == "add":
            (repo / "main.py").write_text('token = "ghp_' + "A" * 40 + '"\n')
        return original(args, **kwargs)
    monkeypatch.setattr(watcher, "_run", inject_before_add)
    old = git(repo, "rev-parse", "HEAD")
    assert watcher.tick(force=True)["status"] == "error"
    assert git(repo, "rev-parse", "HEAD") == old
    assert git(repo, "diff", "--cached") == ""


@pytest.mark.parametrize("content", [
    b'key = "sk-proj-syntheticABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"',
    b'key = "ghp_exampleABCDEFGHIJKLMNOPQRSTUVWXYZ12345"',
    b'password = "YOUR_PASSWORD_HERE"',
])
def test_explicit_fixture_tokens_are_allowed(content: bytes) -> None:
    assert not contains_secret(content)


def test_ordinary_comment_does_not_hide_real_token() -> None:
    assert contains_secret(b'token = "ghp_' + b"A" * 40 + b'" # example request')


def test_symlink_is_not_followed(repo: Path, tmp_path: Path) -> None:
    watcher = configured(repo)
    outside = tmp_path / "outside.py"
    outside.write_text("private\n")
    link = repo / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links require Windows developer-mode privilege")
    result = watcher.tick(force=True)
    assert result["status"] == "blocked"
    assert "linked.py" not in git(repo, "ls-tree", "--name-only", "HEAD")


def test_requires_exact_repo_not_subdirectory_or_parent(repo: Path) -> None:
    (repo / "src").mkdir()
    with pytest.raises(AutosaveError):
        GitAutosave(repo / "src")
    with pytest.raises(AutosaveError):
        GitAutosave(repo.parent)


def test_unconfigured_and_new_configs_never_commit(repo: Path) -> None:
    watcher = GitAutosave(repo)
    (repo / "main.py").write_text("change\n")
    assert watcher.tick(force=True) == {"status": "disabled", "repo": str(repo), "configured": False}
    value = watcher.init_config()
    assert value["enabled"] is False and value["push"] is False
    assert watcher.tick(force=True)["status"] == "disabled"
    assert git(repo, "show", "HEAD:main.py") == "print('initial')"


@pytest.mark.parametrize("config", [
    '{"push":"yes"}', '{"enabled":1}', '{"poll_seconds":0}',
    '{"debounce_seconds":NaN}', '{"unknown":true}', '{"push":true,"push":false}',
])
def test_config_rejects_invalid_values(repo: Path, config: str) -> None:
    watcher = GitAutosave(repo)
    watcher.config_path.write_text(config)
    with pytest.raises(AutosaveError):
        load_config(watcher.config_path)


@pytest.mark.parametrize("url", ["https://github.com/example-user/island.git", "git@github.com:example-user/island.git", "ssh://git@github.com/example-user/island"])
def test_trusted_urls(url: str) -> None:
    assert trusted_github_url(url)


@pytest.mark.parametrize("url", [
    "https://user:secret@github.com/u/r.git", "https://github.com.evil.test/u/r", "file:///tmp/repo",
    "https://github.com/u/r?token=secret", "ssh://user@github.com/u/r", "git@evil.test:u/r", "https://github.com/u/../r",
])
def test_untrusted_urls(url: str) -> None:
    assert not trusted_github_url(url)


def upstream(repo: Path) -> None:
    git(repo, "remote", "add", "origin", "https://github.com/example/autosave-test.git")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")


def test_push_failure_keeps_commit_and_retries_without_new_changes(repo: Path, monkeypatch) -> None:
    now = [100.0]
    watcher = configured(repo, clock=lambda: now[0], push=True)
    upstream(repo)
    original = watcher._run
    pushes = []
    def no_network(args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            assert kwargs["timeout"] <= 30
            return subprocess.CompletedProcess(args, 1, b"", b"credential should never be logged")
        return original(args, **kwargs)
    monkeypatch.setattr(watcher, "_run", no_network)
    (repo / "main.py").write_text("backup\n")
    result = watcher.tick(force=True)
    saved = git(repo, "rev-parse", "HEAD")
    assert result["status"] == "committed" and result["push"] == "failed"
    assert result["push_pending"] is True
    assert git(repo, "status", "--porcelain") == ""
    assert watcher.tick()["push"] == "retry_wait"
    now[0] += 6
    assert watcher.tick()["push"] == "failed"
    assert len(pushes) == 2 and git(repo, "rev-parse", "HEAD") == saved
    assert "--force" not in pushes[0]


def test_push_is_blocked_for_untrusted_remote_and_mismatched_upstream(repo: Path, monkeypatch) -> None:
    watcher = configured(repo, push=True)
    upstream(repo)
    git(repo, "remote", "set-url", "origin", "https://user:do-not-log@github.com/u/r")
    result = watcher.tick(force=True)
    assert result["push"] == "blocked"
    assert "do-not-log" not in json.dumps(result)
    git(repo, "remote", "set-url", "origin", "https://github.com/u/r")
    git(repo, "config", "branch.main.merge", "refs/heads/other")
    assert watcher.tick(force=True)["push"] == "blocked"


def test_successful_push_is_normal_fast_forward_only(repo: Path, monkeypatch) -> None:
    watcher = configured(repo, push=True)
    upstream(repo)
    original = watcher._run
    seen = []
    def fake_push(args, **kwargs):
        if args[0] == "push":
            seen.append(args)
            git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return original(args, **kwargs)
    monkeypatch.setattr(watcher, "_run", fake_push)
    (repo / "main.py").write_text("backup\n")
    assert watcher.tick(force=True)["push"] == "pushed"
    assert seen == [["push", "--porcelain", "origin", "refs/heads/main:refs/heads/main"]]
    assert watcher.tick(force=True)["push"] == "up_to_date"


def test_cli_init_config_status_and_once(repo: Path, capsys) -> None:
    assert main(["--repo", str(repo), "--init-config"]) == 0
    assert json.loads(capsys.readouterr().out)["config"] == DEFAULT_CONFIG
    value = DEFAULT_CONFIG | {"enabled": True, "push": False}
    (repo / ".git" / "island-autosave.json").write_text(json.dumps(value))
    assert main(["--repo", str(repo), "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    (repo / "main.py").write_text("manual snapshot\n")
    assert main(["--repo", str(repo), "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "committed"


@pytest.mark.parametrize("policy", ["signing", "hooks_path", "active_hook"])
def test_user_hook_and_signing_policies_pause_instead_of_bypassing(repo: Path, policy: str) -> None:
    watcher = configured(repo)
    if policy == "signing":
        git(repo, "config", "commit.gpgSign", "true")
    elif policy == "hooks_path":
        git(repo, "config", "core.hooksPath", "custom-hooks")
    else:
        (repo / ".git" / "hooks" / "pre-commit").write_text("must never run")
    old = git(repo, "rev-parse", "HEAD")
    (repo / "main.py").write_text("change\n")
    result = watcher.tick(force=True)
    assert result["status"] == "busy", result
    assert git(repo, "rev-parse", "HEAD") == old


def test_missing_origin_retains_created_commit_result(repo: Path) -> None:
    watcher = configured(repo, push=True)
    (repo / "main.py").write_text("local backup\n")
    result = watcher.tick(force=True)
    assert result["status"] == "committed" and result["push"] == "blocked"
    assert result["commit"] == git(repo, "rev-parse", "HEAD")
    assert git(repo, "status", "--porcelain") == ""


def test_missing_tracking_ref_still_attempts_safe_push(repo: Path, monkeypatch) -> None:
    watcher = configured(repo, push=True)
    upstream(repo)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    original = watcher._run
    pushes = []
    def fake_push(args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"offline")
        return original(args, **kwargs)
    monkeypatch.setattr(watcher, "_run", fake_push)
    result = watcher.tick(force=True)
    assert result["status"] == "clean" and result["push"] == "failed", result
    assert len(pushes) == 1


def test_failed_index_handoff_preserves_prepared_recovery_index(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("saved to commit\n")
    original = os.replace
    def fail_index(source, target):
        if Path(target) == repo / ".git" / "index":
            raise OSError("simulated sharing violation")
        return original(source, target)
    monkeypatch.setattr(os, "replace", fail_index)
    result = watcher.tick(force=True)
    assert result["status"] == "error" and "recovery" in result["reason"]
    assert git(repo, "show", "HEAD:main.py") == "saved to commit"
    assert (repo / ".git" / "index.lock").read_bytes().startswith(b"DIRC")
    assert watcher.tick(force=True)["status"] == "busy"


def test_interrupted_ref_update_retains_prepared_index(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("committed before interrupt\n")
    original = watcher._run
    def interrupted(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "update-ref":
            assert result.returncode == 0
            raise KeyboardInterrupt
        return result
    monkeypatch.setattr(watcher, "_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        watcher.tick(force=True)
    assert git(repo, "show", "HEAD:main.py") == "committed before interrupt"
    assert (repo / ".git" / "index.lock").read_bytes().startswith(b"DIRC")


def test_safe_status_persists_last_success_across_clean_tick(repo: Path, monkeypatch) -> None:
    watcher = configured(repo)
    (repo / "main.py").write_text("backup\n")
    saved = watcher.tick(force=True)
    watcher.tick(force=True)
    state = watcher.status()["last_status"]
    assert state["status"] == "clean"
    assert state["last_commit"] == saved["commit"]
    assert state["last_commit_at"].endswith("Z")
    assert (repo / ".git" / "island-autosave-status.json").stat().st_size < 256 * 1024
    monkeypatch.setattr(watcher, "_tick", lambda **kwargs: {
        "status": "blocked", "files": ["程" * 500] * 2000,
        "skipped_files": [{"path": "檔" * 500, "reason": "x" * 500}] * 2000})
    watcher.tick()
    state = watcher.status()["last_status"]
    assert len(state["files"]) == len(state["skipped_files"]) == 32
    assert state["files_total"] == state["skipped_files_total"] == 2000
    assert (repo / ".git" / "island-autosave-status.json").stat().st_size < 65536


def test_other_instance_is_busy_while_watcher_sleeps(repo: Path, monkeypatch) -> None:
    first = configured(repo)
    second = GitAutosave(repo)
    sleeps = []
    def stop_after_check(seconds):
        sleeps.append(seconds)
        assert second.tick(force=True)["status"] == "busy"
        assert second.watch() == 0
        raise KeyboardInterrupt
    monkeypatch.setattr("tools.git_autosave.time.sleep", stop_after_check)
    with pytest.raises(KeyboardInterrupt):
        first.watch()
    assert sleeps == [2]
    assert second.tick(force=True)["status"] == "clean"
