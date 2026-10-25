"""Install this project's narrow AGY MCP client without changing permissions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from desktop_settings import default_data_dir
from office_bridge_client import BridgeClientError, OfficeBridgeClient

SERVER_NAME = "island-office"
TOOLS = ["island_status", "island_start_collaboration", "island_get_task", "island_cancel_task", "island_review_changes"]


class RemoteSetupError(ValueError):
    pass


def _linked(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read(path):
    if _linked(path):
        raise RemoteSetupError("設定路徑不能是連結。")
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda *_: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise RemoteSetupError("設定不是有效 JSON；已保留原檔，請使用 AGY 的 MCP 管理介面新增。") from None


def _write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="office-config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RemoteSetup:
    def __init__(self, project=PROJECT, data_dir=None, python=None):
        raw = Path(project).expanduser().absolute()
        if _linked(raw) or not raw.is_dir():
            raise RemoteSetupError("請選擇實際的 IslandWorld 專案目錄。")
        self.project = raw.resolve()
        self.data_dir = Path(data_dir).expanduser().absolute() if data_dir is not None else default_data_dir()
        self.folder = self.project / ".agents"
        if _linked(self.folder):
            raise RemoteSetupError("專案 MCP 設定目錄不能是連結。")
        self.config = self.folder / "mcp_config.json"
        self.marker = self.folder / "island-office-managed.json"
        candidate = self.project / ".venv-desktop" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.python = Path(python) if python else candidate if candidate.is_file() else Path(sys.executable)

    def definition(self):
        script = self.project / "tools" / "island_mcp.py"
        if (not script.is_file() or _linked(script) or _linked(script.parent)
                or not script.resolve().is_relative_to(self.project) or not self.python.is_file()):
            raise RemoteSetupError("找不到橋接程式或 Python，請先完整安裝專案。")
        return {"command": str(self.python.resolve()),
                "args": [str(script), "--project", str(self.project), "--data-dir", str(self.data_dir)],
                "cwd": str(self.project), "enabledTools": TOOLS, "timeoutSeconds": 60}

    def setup(self):
        definition = self.definition()
        config = _read(self.config) or {}
        servers = config.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise RemoteSetupError("mcpServers 格式不正確，已保留設定。")
        previous = servers.get(SERVER_NAME)
        marker = _read(self.marker)
        if previous is not None and previous != definition:
            if (not marker or marker.get("workspace") != str(self.project)
                    or marker.get("server_hash") != _hash(previous)):
                raise RemoteSetupError("已有同名的自訂 MCP 伺服器，已保留原設定。")
        self.folder.mkdir(exist_ok=True)
        config["mcpServers"] = {**servers, SERVER_NAME: definition}
        _write(self.config, config)
        _write(self.marker, {"version": 1, "workspace": str(self.project), "server_hash": _hash(definition)})
        return {"configured": True, "server": SERVER_NAME, "project": str(self.project),
                "permissions_changed": False, "tools": TOOLS}

    def status(self):
        config = _read(self.config) or {}
        servers = config.get("mcpServers")
        configured = isinstance(servers, dict) and servers.get(SERVER_NAME) == self.definition()
        result = {"configured": configured, "server": SERVER_NAME, "project": str(self.project),
                  "office_ready": False}
        try:
            state = OfficeBridgeClient(self.project, self.data_dir).status()
            result.update(office_ready=True, office=state)
        except BridgeClientError as failure:
            result.update(code=failure.code, message=str(failure))
        return result


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "status"])
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    try:
        setup = RemoteSetup(args.project, args.data_dir)
        result = setup.setup() if args.action == "setup" else setup.status()
    except (RemoteSetupError, OSError):
        print(json.dumps({"configured": False, "message": "無法安全設定本機 MCP；已保留既有設定。"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
