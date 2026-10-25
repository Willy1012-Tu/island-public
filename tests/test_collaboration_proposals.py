"""Host-owned copy edits, strict proposal scope and transactional failure tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import collaboration_proposals as proposals
from collaboration_workspace import ArtifactError


@pytest.fixture
def working(tmp_path):
    root = tmp_path / "host-owned-copy"
    root.mkdir()
    (root / "main.py").write_bytes(b"value = 1\n")
    (root / "remove.txt").write_bytes(b"old text\n")
    return root


def change(path="main.py", operation="replace", content="value = 2\n"):
    return {"path": path, "operation": operation, "content": content}


def output(*changes, summary="已完成工作副本修改。"):
    return json.dumps({"summary": summary, "changes": list(changes)}, ensure_ascii=False)


def test_schema_contract_requires_all_fields_and_does_not_offer_arbitrary_commands():
    schema = proposals.PROPOSAL_SCHEMA
    assert set(schema["required"]) == {"summary", "changes"}
    assert schema["additionalProperties"] is False
    item = schema["properties"]["changes"]["items"]
    assert set(item["required"]) == {"path", "operation", "content"}
    assert item["additionalProperties"] is False
    assert item["properties"]["operation"]["enum"] == ["create", "replace", "delete"]


def test_create_replace_delete_only_touch_the_supplied_copy_and_return_actual_paths(working, tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "main.py").write_bytes(b"value = 1\n")
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
        result = proposals.apply_proposals(working, output(
            change(), change("src/新 功能.py", "create", "print('小島')\n"), change("remove.txt", "delete", ""),
        ))
    assert result == {"summary": "已完成工作副本修改。", "changed_files": ["main.py", "src/新 功能.py", "remove.txt"]}
    assert (working / "main.py").read_bytes() == b"value = 2\n"
    assert (working / "src" / "新 功能.py").read_text(encoding="utf-8") == "print('小島')\n"
    assert not (working / "remove.txt").exists()
    assert (original / "main.py").read_bytes() == b"value = 1\n"
    run.assert_not_called(); popen.assert_not_called()


def test_empty_create_and_matching_create_are_idempotent(working):
    first = proposals.apply_proposals(working, output(change("empty.py", "create", "")))
    assert first["changed_files"] == ["empty.py"] and (working / "empty.py").read_bytes() == b""
    with patch("collaboration_proposals.os.replace") as replace:
        second = proposals.apply_proposals(working, output(change("empty.py", "create", "")))
    assert second["changed_files"] == []
    replace.assert_not_called()


@pytest.mark.parametrize("second", [
    change("../outside.py", "create", "outside"), change("main.py", "create", "not the same"),
    change("missing.py", "replace", "x"), change("missing.py", "delete", ""),
    change("remove.txt", "delete", "must be empty"), change("remove.txt", "execute", "bad"),
    {"path": "new.py", "operation": "create", "content": []},
    {"path": "new.py", "operation": "create", "content": "x", "command": "must not run"},
])
def test_invalid_second_change_prevents_every_write(working, second):
    before = {path.name: path.read_bytes() for path in working.iterdir()}
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, output(change(), second))
    assert {path.name: path.read_bytes() for path in working.iterdir()} == before


@pytest.mark.parametrize("path", [
    "../outside.py", "/outside.py", "C:/outside.py", "a/../../outside.py", "a\\outside.py",
    ".env", "src/.hidden.py", "src/credentials.json", "tokens/main.py", "private/file.py",
    "node_modules/a.js", "BUILD/main.py", "src/artifacts/result.py", "src/NODE_MODULES/a.py",
    "line\nname.py", "line\x00name.py", "image.png", "src/con.py", "folder /main.py", "dir./main.py",
])
def test_hidden_secret_generated_traversal_and_windows_alias_paths_are_rejected(working, path):
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, output(change(path, "create", "unsafe")))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("raw", [
    "```json\n{}\n```", "{} trailing", "[]", "null", "",
    '{"summary":"a","summary":"b","changes":[]}',
    '{"summary":"a","changes":[{"path":"x.py","path":"y.py","operation":"create","content":"x"}]}',
    '{"summary":"a","changes":[{"path":"x.py","operation":"create","content":NaN}]}',
])
def test_entire_json_must_be_strict_and_unambiguous(working, raw):
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, raw)
    assert (working / "main.py").read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("data", [
    {"summary": "", "changes": [change()]}, {"summary": " \n", "changes": [change()]},
    {"summary": 12, "changes": [change()]}, {"summary": "x" * 8001, "changes": [change()]},
    {"summary": "valid", "changes": []}, {"summary": "valid", "changes": "not array"},
    {"summary": "valid", "changes": [change()], "extra": "not allowed"},
    {"summary": "valid", "changes": [change(f"file{i}.py", "create", "x") for i in range(13)]},
])
def test_required_fields_summary_and_change_count_limits(working, data):
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, json.dumps(data))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_output_limit_is_utf8_bytes_and_not_character_count(monkeypatch, working):
    text = output(change(content="島" * 100))
    monkeypatch.setattr(proposals, "MAX_OUTPUT_BYTES", len(text.encode("utf-8")) - 1)
    assert len(text) < proposals.MAX_OUTPUT_BYTES
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, text)
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_duplicate_paths_include_windows_case_aliases(working):
    with pytest.raises(ArtifactError, match="重複"):
        proposals.apply_proposals(working, output(change(), change("MAIN.PY", "replace", "other")))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_content_and_existing_targets_must_be_bounded_utf8_text(monkeypatch, working):
    monkeypatch.setattr(proposals, "MAX_FILE_BYTES", 16)
    with pytest.raises(ArtifactError, match="大小"):
        proposals.apply_proposals(working, output(change(content="x" * 17)))
    (working / "large.py").write_bytes(b"x" * 17)
    with pytest.raises(ArtifactError, match="大小"):
        proposals.apply_proposals(working, output(change("large.py", "replace", "short")))
    (working / "large.py").unlink()
    (working / "binary.py").write_bytes(b"\xffnot utf8")
    with pytest.raises(ArtifactError, match="UTF-8"):
        proposals.apply_proposals(working, output(change()))


def test_invalid_unicode_in_content_is_rejected_before_any_copy_write(working):
    data = {"summary": "valid", "changes": [change(), change("other.py", "create", "\ud800")]}
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, json.dumps(data))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_final_inventory_count_and_total_size_are_checked_before_any_write(monkeypatch, working):
    monkeypatch.setattr(proposals, "MAX_FILES", 2)
    with pytest.raises(ArtifactError, match="數量"):
        proposals.apply_proposals(working, output(change(), change("third.py", "create", "x")))
    assert (working / "main.py").read_bytes() == b"value = 1\n"
    monkeypatch.setattr(proposals, "MAX_FILES", 2000)
    monkeypatch.setattr(proposals, "MAX_TOTAL_BYTES", 20)
    with pytest.raises(ArtifactError, match="總大小"):
        proposals.apply_proposals(working, output(change(content="x" * 20)))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_deletion_can_make_room_within_the_final_inventory_limit(monkeypatch, working):
    monkeypatch.setattr(proposals, "MAX_FILES", 2)
    result = proposals.apply_proposals(working, output(change("remove.txt", "delete", ""), change("new.py", "create", "x")))
    assert result["changed_files"] == ["remove.txt", "new.py"]
    assert not (working / "remove.txt").exists() and (working / "new.py").read_bytes() == b"x"


def test_root_link_and_eligible_junction_output_are_rejected(monkeypatch, working):
    linked = proposals._linked
    monkeypatch.setattr(proposals, "_linked", lambda path: path == working or linked(path))
    with pytest.raises(ArtifactError, match="連結"):
        proposals.apply_proposals(working, output(change()))
    monkeypatch.setattr(proposals, "_linked", linked)
    actual = getattr(Path, "is_junction", None)
    monkeypatch.setattr(Path, "is_junction", lambda path: True if path == working / "main.py" else actual(path) if actual else False, raising=False)
    with pytest.raises(ArtifactError, match="連結"):
        proposals.apply_proposals(working, output(change()))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_directory_target_or_file_parent_is_invalid_before_first_write(working):
    (working / "folder.py").mkdir()
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, output(change(), change("folder.py", "replace", "x")))
    with pytest.raises(ArtifactError):
        proposals.apply_proposals(working, output(change(), change("remove.txt/child.py", "create", "x")))
    assert (working / "main.py").read_bytes() == b"value = 1\n"


def test_second_commit_failure_rolls_back_replacement_and_removes_staged_files(working):
    actual = os.replace

    def fail_second(source, target):
        if Path(target) == working / "new.py":
            raise OSError("fixture commit failure")
        return actual(source, target)

    with patch("collaboration_proposals.os.replace", side_effect=fail_second), pytest.raises(ArtifactError, match="已回復"):
        proposals.apply_proposals(working, output(change(), change("new.py", "create", "x")))
    assert (working / "main.py").read_bytes() == b"value = 1\n"
    assert not (working / "new.py").exists()
    assert not list(working.glob(".agentdesk-*"))


def test_failed_create_restores_an_earlier_deleted_file(working):
    actual = os.replace

    def fail_new(source, target):
        if Path(target) == working / "new.py":
            raise OSError("fixture commit failure")
        return actual(source, target)

    with patch("collaboration_proposals.os.replace", side_effect=fail_new), pytest.raises(ArtifactError):
        proposals.apply_proposals(working, output(change("remove.txt", "delete", ""), change("new.py", "create", "x")))
    assert (working / "remove.txt").read_bytes() == b"old text\n"
    assert not (working / "new.py").exists()


def test_rollback_does_not_overwrite_another_writer_edit(working):
    actual = os.replace

    def fail_and_edit(source, target):
        if Path(target) == working / "new.py":
            (working / "main.py").write_bytes(b"another writer's copy edit\n")
            raise OSError("fixture concurrent edit")
        return actual(source, target)

    with patch("collaboration_proposals.os.replace", side_effect=fail_and_edit), pytest.raises(ArtifactError, match="回復未完成") as error:
        proposals.apply_proposals(working, output(change(), change("new.py", "create", "x")))
    assert error.value.status == 500
    assert (working / "main.py").read_bytes() == b"another writer's copy edit\n"
    assert not list(working.glob(".agentdesk-*"))
