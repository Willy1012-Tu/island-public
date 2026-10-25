"""Actual temporary source edits, review, conflicts and recovery before apply."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from collaboration_workspace import ArtifactError, ArtifactStore, CollaborationWorkspace

RUN = "a" * 32


def source(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_bytes(b"value = 1\n")
    (project / "remove.txt").write_bytes(b"old\n")
    (project / ".env").write_text("DO_NOT_COPY=private")
    return project


def artifact(project):
    store = ArtifactStore(project)
    with CollaborationWorkspace(project) as workspace:
        (workspace.working / "main.py").write_bytes(b"value = 2\n")
        (workspace.working / "remove.txt").unlink()
        (workspace.working / "new.py").write_bytes(b"print('ready')")
        public = store.create(RUN, workspace.baseline, workspace.working)
    return store, public


def test_source_copy_shared_edits_are_reviewable_and_only_apply_changes_original(tmp_path):
    project = source(tmp_path)
    with CollaborationWorkspace(project) as workspace:
        working = workspace.working
        assert not (working / ".env").exists()
        (working / "main.py").write_bytes(b"value = 2\n")
        (working / "new.py").write_bytes(b"print('ready')")
        (working / "remove.txt").unlink()
        store = ArtifactStore(project)
        public = store.create(RUN, workspace.baseline, working)
        assert (project / "main.py").read_bytes() == b"value = 1\n"
        assert public["applied"] is False
        assert {item["status"] for item in public["changed_files"]} == {"added", "modified", "deleted"}
    assert not working.exists()
    review = ArtifactStore(project).review(RUN)  # Survives process/store recreation.
    assert review["artifact"] == public
    assert "value = 2" in review["diff"] and "No newline at end of file" in review["diff"]
    applied = store.apply(RUN)
    assert applied["applied"] is True
    assert (project / "main.py").read_bytes() == b"value = 2\n"
    assert (project / "new.py").read_bytes() == b"print('ready')"
    assert not (project / "remove.txt").exists()
    assert (project / ".env").read_text() == "DO_NOT_COPY=private"
    assert store.apply(RUN) == applied  # Idempotent, does not overwrite later user edits.


def test_any_baseline_conflict_prevents_all_original_writes(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    (project / "remove.txt").write_bytes(b"user edit\n")
    with pytest.raises(ArtifactError, match="原專案已修改") as error:
        store.apply(RUN)
    assert error.value.status == 409
    assert (project / "main.py").read_bytes() == b"value = 1\n"
    assert not (project / "new.py").exists()
    assert (project / "remove.txt").read_bytes() == b"user edit\n"


def test_added_file_cannot_overwrite_new_user_file(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    (project / "new.py").write_bytes(b"user owns this\n")
    with pytest.raises(ArtifactError):
        store.apply(RUN)
    assert (project / "new.py").read_bytes() == b"user owns this\n"
    assert (project / "main.py").read_bytes() == b"value = 1\n"


def test_tampered_payload_or_patch_cannot_be_applied(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    folder = project / "artifacts" / "collaborations" / RUN
    (folder / "0.new").write_bytes(b"altered payload\n")
    with pytest.raises(ArtifactError, match="內容已變更"):
        store.apply(RUN)
    assert (project / "main.py").read_bytes() == b"value = 1\n"
    (folder / "changes.patch").write_text("altered patch")
    with pytest.raises(ArtifactError):
        store.review(RUN)


def test_changing_payload_and_manifest_hash_cannot_apply_unreviewed_bytes(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    folder = project / "artifacts" / "collaborations" / RUN
    altered = b"unreviewed = True\n"
    (folder / "0.new").write_bytes(altered)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["after_hash"] = hashlib.sha256(altered).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="不一致"):
        store.apply(RUN)
    assert (project / "main.py").read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("path", ["../outside.py", "/outside.py", "C:/outside.py", "a/../../outside.py", "a\\outside.py", ".env", "line\nname.py"])
def test_malformed_manifest_paths_never_escape_original(tmp_path, path):
    project = source(tmp_path)
    store, _ = artifact(project)
    manifest_path = project / "artifacts" / "collaborations" / RUN / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError):
        store.apply(RUN)
    assert (project / "main.py").read_bytes() == b"value = 1\n"


def test_failed_commit_rolls_back_previously_written_original_files(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    replace = os.replace

    def fail_new(source_path, target):
        if Path(target) == project / "new.py":
            raise OSError("simulated second-file failure")
        return replace(source_path, target)

    with patch("collaboration_workspace.os.replace", side_effect=fail_new), pytest.raises(ArtifactError, match="已回復"):
        store.apply(RUN)
    assert (project / "main.py").read_bytes() == b"value = 1\n"
    assert (project / "remove.txt").exists() and not (project / "new.py").exists()
    assert not store.review(RUN)["artifact"]["applied"]


def test_failed_commit_preserves_an_external_edit_instead_of_overwriting_it_during_rollback(tmp_path):
    project = source(tmp_path)
    store, _ = artifact(project)
    replace = os.replace
    def concurrent_edit(source_path, target):
        if Path(target) == project / "new.py":
            (project / "main.py").write_bytes(b"external edit after commit\n")
            raise OSError("simulated commit failure")
        return replace(source_path, target)
    with patch("collaboration_workspace.os.replace", side_effect=concurrent_edit), pytest.raises(ArtifactError, match="回復未完成"):
        store.apply(RUN)
    assert (project / "main.py").read_bytes() == b"external edit after commit\n"
    assert not (project / "new.py").exists()


def test_empty_files_and_unicode_paths_have_downloadable_git_patch_metadata(tmp_path):
    project = source(tmp_path)
    store = ArtifactStore(project)
    with CollaborationWorkspace(project) as workspace:
        (workspace.working / "空 白.py").write_bytes(b"")
        store.create(RUN, workspace.baseline, workspace.working)
    assert "new file mode 100644" in store.patch(RUN)
    assert '"a/空 白.py"' in store.patch(RUN)
    store.apply(RUN)
    assert (project / "空 白.py").read_bytes() == b""


def test_worker_link_is_rejected_instead_of_becoming_a_deletion(tmp_path):
    project = source(tmp_path)
    with CollaborationWorkspace(project) as workspace:
        target = workspace.working / "main.py"
        target.unlink()
        try:
            target.symlink_to(project / "main.py")
        except (OSError, NotImplementedError):
            pytest.skip("Creating symlinks requires Windows developer privileges")
        with pytest.raises(ArtifactError, match="連結"):
            ArtifactStore(project).create(RUN, workspace.baseline, workspace.working)
    assert (project / "main.py").read_bytes() == b"value = 1\n"


def test_artifact_identifier_cannot_choose_an_arbitrary_directory(tmp_path):
    store = ArtifactStore(source(tmp_path))
    for identifier in ("../other", "", "Z" * 32, None):
        with pytest.raises(ArtifactError):
            store.review(identifier)


def test_another_office_cannot_apply_while_workspace_os_lock_is_owned(tmp_path):
    project = source(tmp_path)
    owner, _ = artifact(project)
    with owner._workspace_lock():
        with pytest.raises(ArtifactError, match="正在套用") as error:
            ArtifactStore(project).apply(RUN)
        assert error.value.status == 409
        assert (project / "main.py").read_bytes() == b"value = 1\n"
    assert ArtifactStore(project).apply(RUN)["applied"] is True
