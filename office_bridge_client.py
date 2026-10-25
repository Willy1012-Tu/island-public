"""A project-bound client for the desktop owner's private loopback bridge."""
from __future__ import annotations

import hmac
import json
import re
from pathlib import Path
from urllib import error, request

from desktop_settings import default_data_dir

_JOB = re.compile(r"[a-f0-9]{32}\Z")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]{24,128}\Z")
_MAX_RESPONSE = 4 * 1024 * 1024


class BridgeClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda *_: (_ for _ in ()).throw(ValueError()))
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


class OfficeBridgeClient:
    def __init__(self, project: Path | str, data_dir: Path | str | None = None):
        raw = Path(project).expanduser().absolute()
        if _linked(raw) or not raw.is_dir():
            raise BridgeClientError("invalid_project", "請使用設定好的 IslandWorld 專案目錄。")
        self.project = raw.resolve()
        self.data_dir = Path(data_dir) if data_dir is not None else default_data_dir()
        self.record_path = self.data_dir / "office-bridge.json"
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def _record(self) -> dict:
        try:
            if any(_linked(path) for path in (self.record_path, self.data_dir, *self.data_dir.parents)):
                raise ValueError()
            with self.record_path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError()
            record = _object(raw)
            if (type(record.get("version")) is not int or record["version"] != 1
                    or type(record.get("port")) is not int or not 1 <= record["port"] <= 65535
                    or not isinstance(record.get("workspace"), str)
                    or Path(record["workspace"]).resolve() != self.project
                    or any(not isinstance(record.get(key), str) or not _CAPABILITY.fullmatch(record[key])
                           for key in ("token", "nonce"))):
                raise ValueError()
            return record
        except FileNotFoundError:
            raise BridgeClientError("office_not_ready", "本機辦公室尚未啟用遠端橋接；請從系統匣結束並重新開啟 Agent Desk。") from None
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            raise BridgeClientError("invalid_discovery", "本機橋接資訊無效或不屬於此專案，請重新開啟 Agent Desk。") from None

    def _http(self, record: dict, path: str, data: dict | None = None) -> dict:
        origin = f"http://127.0.0.1:{record['port']}"
        headers = {"Authorization": "Bearer " + record["token"], "Accept": "application/json", "Origin": origin}
        payload = None
        if data is not None:
            try:
                payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, TypeError, UnicodeError):
                raise BridgeClientError("invalid_arguments", "工具參數必須是有效 JSON。") from None
            if len(payload) > 64 * 1024:
                raise BridgeClientError("invalid_arguments", "工具參數超過大小限制。")
            headers["Content-Type"] = "application/json"
        message = request.Request(origin + path, data=payload, headers=headers)
        try:
            with self._opener.open(message, timeout=10) as response:
                raw = response.read(_MAX_RESPONSE + 1)
                if len(raw) > _MAX_RESPONSE:
                    raise ValueError()
                return _object(raw)
        except error.HTTPError as failure:
            code = "bridge_unauthorized" if failure.code in (401, 403) else "office_rejected"
            status = failure.code
            failure.close()
            # Do not echo server text, a URL, headers, or a bearer value.
            raise BridgeClientError(code, f"本機辦公室拒絕要求（HTTP {status}）；請查詢目前狀態後再操作。") from None
        except (error.URLError, TimeoutError, OSError):
            message = ("本機辦公室無法連線；請確認 Agent Desk 正在運作。"
                       if data is None else "未能確認要求結果；請先查詢任務狀態，避免重複交辦。")
            raise BridgeClientError("office_unavailable", message) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BridgeClientError("invalid_response", "本機辦公室回覆格式無效。") from None

    def _owner(self) -> dict:
        record = self._record()
        info = self._http(record, "/api/bridge/info")
        if (type(info.get("version")) is not int or info["version"] != 1 or not isinstance(info.get("workspace"), str)
                or Path(info["workspace"]).resolve() != self.project
                or not isinstance(info.get("nonce"), str)
                or not hmac.compare_digest(info["nonce"], record["nonce"])):
            raise BridgeClientError("owner_mismatch", "本機辦公室身分不符，未送出任何動作。")
        return record

    @staticmethod
    def _job_id(value: str) -> str:
        if not isinstance(value, str) or not _JOB.fullmatch(value):
            raise BridgeClientError("invalid_arguments", "請提供有效的任務識別碼。")
        return value

    def status(self) -> dict:
        record = self._owner()
        return self._http(record, "/api/bridge/status")

    def start(self, arguments: dict) -> dict:
        if (not isinstance(arguments, dict) or not {"provider", "prompt", "collaboration"} <= set(arguments)
                or not set(arguments) <= {"provider", "prompt", "model", "reasoning_effort", "collaboration"}):
            raise BridgeClientError("invalid_arguments", "協作需要主責、需求與成員，不能覆寫網址或工作目錄。")
        return self._http(self._owner(), "/api/bridge/start", arguments)

    def get_task(self, job_id: str) -> dict:
        job_id = self._job_id(job_id)
        return self._http(self._owner(), "/api/bridge/jobs/" + job_id)

    def cancel(self, job_id: str) -> dict:
        job_id = self._job_id(job_id)
        return self._http(self._owner(), "/api/bridge/jobs/" + job_id + "/cancel", {})

    def review(self, job_id: str) -> dict:
        job_id = self._job_id(job_id)
        return self._http(self._owner(), "/api/bridge/jobs/" + job_id + "/changes")
