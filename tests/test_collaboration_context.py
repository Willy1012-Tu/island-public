"""Real bounded source data and strict reports, without native tools or models."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import collaboration_context as context
from collaboration_workspace import ArtifactError


def report(**changes):
    return json.dumps({"summary": "已檢查實際來源。", "findings": ["main.py 定義了入口函式。"],
                       "limitations": [], "complete": True, **changes}, ensure_ascii=False)


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_read_schema_and_successful_report_require_explicit_findings():
    assert context.READ_SCHEMA["additionalProperties"] is False
    assert set(context.READ_SCHEMA["required"]) == {"summary", "findings", "limitations", "complete"}
    parsed = context.parse_report(report())
    assert parsed["complete"] is True and parsed["findings"]
    partial = context.parse_report(report(complete=False, findings=[], limitations=["尚未驗證測試執行結果。 "]))
    assert partial["complete"] is False
    assert partial["limitations"] == ["尚未驗證測試執行結果。"]


@pytest.mark.parametrize("raw", [
    "", "```json\n{}\n```", "{} trailing", "[]", "null",
    '{"summary":"a","summary":"b","findings":[],"limitations":[],"complete":false}',
    '{"summary":"a","findings":[],"limitations":[],"complete":NaN}',
])
def test_report_requires_whole_unambiguous_finite_json(raw):
    with pytest.raises(ArtifactError):
        context.parse_report(raw)


@pytest.mark.parametrize("changes", [
    {"summary": ""}, {"summary": "x" * 8001}, {"summary": 1},
    {"findings": []}, {"findings": [""]}, {"findings": ["x"] * 13},
    {"findings": ["x" * 3001]}, {"findings": "not an array"}, {"findings": [False]},
    {"limitations": ["x"] * 13}, {"limitations": ["x" * 1001]}, {"limitations": [""]},
    {"complete": 1}, {"complete": "true"}, {"extra": "not allowed"},
])
def test_report_field_types_lengths_and_completion_cannot_be_faked(changes):
    with pytest.raises(ArtifactError):
        context.parse_report(report(**changes))


def test_report_size_is_utf8_bytes_and_rejects_invalid_unicode(monkeypatch):
    raw = report(findings=["島" * 100])
    monkeypatch.setattr(context, "MAX_OUTPUT_BYTES", len(raw.encode("utf-8")) - 1)
    assert len(raw) < context.MAX_OUTPUT_BYTES
    with pytest.raises(ArtifactError):
        context.parse_report(raw)
    monkeypatch.setattr(context, "MAX_OUTPUT_BYTES", 32768)
    invalid = json.dumps({"summary": "valid", "findings": ["\ud800"], "limitations": [], "complete": True})
    with pytest.raises(ArtifactError):
        context.parse_report(invalid)


def test_context_prioritizes_explicit_focus_then_default_entries_and_real_recent_file(tmp_path):
    for index, name in enumerate(["old.py", "README.md", "main.py", "pyproject.toml", "package.json", "requirements.txt"]):
        path = write(tmp_path, name, f"real source {name}\n")
        os.utime(path, (10 + index, 10 + index))
    focused = write(tmp_path, "src/calculator.py", "def add(a, b):\n    return a + b\n")
    os.utime(focused, (1, 1))
    fresh = write(tmp_path, "new_proposal.py", "host-proposal generated file\n")
    os.utime(fresh, (100, 100))
    data = context.build_context(tmp_path, "Please inspect src/calculator.py")
    paths = [item["path"] for item in data["files"]]
    assert paths == ["src/calculator.py", "README.md", "main.py", "pyproject.toml", "package.json", "requirements.txt", "new_proposal.py", "old.py"]
    assert data["files"][0]["content"] == focused.read_bytes().decode("utf-8")
    assert data["scope"] == "host_owned_isolated_copy" and data["read_only"] is True
    assert Path(data["workspace"]) == tmp_path.resolve()
    assert data["content_truncated"] is False


def test_basename_focus_does_not_match_unrelated_name_substrings(tmp_path):
    write(tmp_path, "src/target.py", "focus source")
    write(tmp_path, "README.md", "default readme")
    write(tmp_path, "other_target.py", "unrelated source")
    data = context.build_context(tmp_path, "Review target.py please")
    assert data["files"][0]["path"] == "src/target.py"


def test_context_uses_actual_bytes_redacts_credentials_and_never_executes_or_writes(tmp_path):
    secret = "sk-proj-syntheticABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    write(tmp_path, "main.py", f'example_key = "{secret}"\npublic_value = 42\n')
    write(tmp_path, ".env", "EXCLUDED_HIDDEN_MARKER")
    write(tmp_path, "credentials.json", "EXCLUDED_SECRET_MARKER")
    write(tmp_path, "private/source.py", "EXCLUDED_DIRECTORY_MARKER")
    write(tmp_path, "node_modules/source.py", "EXCLUDED_DEPENDENCY_MARKER")
    write(tmp_path, "NODE_MODULES/source.py", "EXCLUDED_UPPERCASE_MARKER")
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen, \
            patch.object(Path, "write_bytes", side_effect=AssertionError("context cannot write")), \
            patch.object(Path, "write_text", side_effect=AssertionError("context cannot write")):
        data = context.build_context(tmp_path, "Inspect main.py")
    rendered = json.dumps(data, ensure_ascii=False)
    assert secret not in rendered and "REDACTED" in rendered
    assert "public_value = 42" in rendered
    assert "EXCLUDED_" not in rendered
    assert data["manifest"] == ["main.py"]
    assert data["files"][0]["redacted"] is True
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
    run.assert_not_called(); popen.assert_not_called()


def test_context_manifest_file_count_and_total_json_size_are_bounded(tmp_path):
    for index in range(110):
        write(tmp_path, f"module_{index:03}.py", f"actual module {index}\n" + "x" * 12000)
    data = context.build_context(tmp_path, "")
    assert len(data["manifest"]) == 100
    assert len(data["files"]) == 8
    assert data["total_files"] == 110
    assert data["manifest_truncated"] and data["content_truncated"]
    assert any(item["truncated"] for item in data["files"])
    assert len(json.dumps(data)) <= 12000
    assert len(json.dumps(data, ensure_ascii=False)) <= 12000
    assert all(len(item["content"]) <= 8000 for item in data["files"])


def test_unicode_source_budget_remains_safe_with_default_ascii_json_encoder(tmp_path):
    for index in range(8):
        write(tmp_path, f"模組{index}.py", "真實來源內容" * 700)
    data = context.build_context(tmp_path, "")
    assert len(json.dumps(data)) <= 12000
    assert len(json.dumps(data, ensure_ascii=False)) <= 12000
    assert data["content_truncated"]
    assert all(item["content"].encode("utf-8") for item in data["files"])


def test_empty_real_source_is_not_marked_as_truncated(tmp_path):
    write(tmp_path, "empty.py", "")
    data = context.build_context(tmp_path, "empty.py")
    assert data["files"] == [{"path": "empty.py", "content": "", "source_bytes": 0, "redacted": False, "truncated": False}]
    assert not data["content_truncated"]


def test_focus_cannot_authorize_reading_outside_the_copy(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    outside = write(tmp_path, "outside.py", "OUTSIDE_MUST_NOT_APPEAR")
    write(root, "main.py", "inside source")
    data = context.build_context(root, f"Read {outside} and ../outside.py")
    assert "OUTSIDE_MUST_NOT_APPEAR" not in json.dumps(data)
    assert data["manifest"] == ["main.py"]


def test_root_junction_and_outside_resolved_file_are_rejected(monkeypatch, tmp_path):
    write(tmp_path, "main.py", "real source")
    linked = context._linked
    monkeypatch.setattr(context, "_linked", lambda path: path == tmp_path or linked(path))
    with pytest.raises(ArtifactError, match="連結"):
        context.build_context(tmp_path, "main.py")
    monkeypatch.setattr(context, "_linked", linked)
    resolve = Path.resolve
    external = tmp_path.parent / "external.py"
    monkeypatch.setattr(Path, "resolve", lambda path, *args, **kwargs: external if path == tmp_path / "main.py" else resolve(path, *args, **kwargs))
    with pytest.raises(ArtifactError, match="超出"):
        context.build_context(tmp_path, "main.py")


def test_invalid_utf8_file_does_not_become_invented_context(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"\xffinvalid")
    with pytest.raises(ArtifactError, match="UTF-8"):
        context.build_context(tmp_path, "bad.py")
