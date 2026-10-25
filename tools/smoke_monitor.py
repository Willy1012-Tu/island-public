"""Run one short REAL monitored task per selected, locally authenticated CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from monitor_service import MonitorService, TERMINAL


async def check(providers: list[str], timeout: float) -> int:
    empty_workspace = tempfile.TemporaryDirectory(prefix="island-connection-check-")
    monitor = MonitorService(Path(empty_workspace.name), timeout=timeout)
    job_ids = []
    try:
        for provider in providers:
            prompt = "這是連線測試。不要使用工具，不要讀取任何檔案。請只回覆：連線正常。"
            job_ids.append(monitor.start_job(provider, prompt)["id"])
        while True:
            snapshot = monitor.snapshot()
            jobs = [job for job in snapshot["jobs"] if job["id"] in job_ids]
            if len(jobs) == len(job_ids) and all(job["status"] in TERMINAL for job in jobs):
                break
            await asyncio.sleep(0.1)
        result = [{"provider": job["provider"], "status": job["status"], "model": job["model"],
                   "elapsed_seconds": job["elapsed_seconds"], "error": job["error"],
                   "event_kinds": dict(Counter(event["kind"] for event in job["events"])),
                   "tools": sorted({event["tool"] for event in job["events"] if event.get("tool")}),
                   "reply": job["latest_message"]} for job in jobs]
        destination = PROJECT / "artifacts"
        destination.mkdir(exist_ok=True)
        (destination / "monitor_smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(job["status"] == "completed" for job in jobs) else 1
    finally:
        await monitor.close()
        empty_workspace.cleanup()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", nargs="+", choices=("codex", "claude", "antigravity"), default=["codex"])
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    return asyncio.run(check(args.providers, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
