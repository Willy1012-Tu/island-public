"""Real CLI teamwork checks using only a tiny, disposable sample project."""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from collaboration_workspace import ArtifactStore
from monitor_service import MonitorService, TERMINAL


async def wait_finished(monitor, parent_id):
    previous = None
    while True:
        snapshot = monitor.snapshot()
        job = next(item for item in snapshot["jobs"] if item["id"] == parent_id)
        progress = (job["collaboration"]["phase"], job["collaboration"]["round"], tuple(
            (item["provider"], item["role"]) for item in snapshot["jobs"]
            if item.get("parent_job_id") == parent_id and item["status"] in ("running", "queued")))
        if progress != previous:
            print(json.dumps({"event": "collaboration_progress", "phase": progress[0], "round": progress[1],
                              "active_turns": progress[2]}, ensure_ascii=False), flush=True)
            previous = progress
        if job["status"] in TERMINAL:
            return job
        await asyncio.sleep(0.1)


async def check(direction: str, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="island-team-check-") as folder:
        workspace = Path(folder)
        original_readme = "Disposable collaboration test. Python arithmetic: 2 + 3 = 5.\n"
        (workspace / "README.md").write_text(original_readme, encoding="utf-8")
        monitor = MonitorService(workspace, timeout=timeout)
        parent_id = None
        try:
            if direction == "agy-to-codex":
                lead = "antigravity"
                members = [{"provider": "codex", "model": "gpt-6.1-sol", "reasoning_effort": "low"},
                           {"provider": "claude", "model": "claude-sonnet-4-6"}]
                prompt = (
                    "This is a small disposable collaboration integration test, not a real project. "
                    "First delegate to Codex one write task: create team_demo.py containing only def add(a, b): return a + b. "
                    "Then delegate to Claude a read-only review of that same file. Do not delegate additional implementation tasks. "
                    "After both actual results, finish with a short honest summary. Do not use the network, install packages, "
                    "or access anything outside the working directory. The only required output file is team_demo.py. "
                    "If Python is unavailable report that limit; do not install it."
                )
            else:
                lead = "codex"
                members = [{"provider": "antigravity", "model": "gemini-3.8-flash-high"}]
                prompt = (
                    "This is a disposable collaboration integration test. Delegate exactly one READ-ONLY task to Antigravity: "
                    "read README.md, analyze the arithmetic statement, and report whether it is correct. "
                    "Then integrate its actual reply and finish. Do not write files, use the network, install anything, "
                    "or access files outside the working directory."
                )
            choices = {"model": "gpt-6.1-sol", "reasoning_effort": "low"} if lead == "codex" else {}
            parent = monitor.start_job(lead, prompt, collaboration={"members": members}, **choices)
            parent_id = parent["id"]
            try:
                job = await asyncio.wait_for(wait_finished(monitor, parent_id), timeout=timeout * 5)
            except asyncio.TimeoutError:
                monitor.cancel_job(parent_id)
                job = await wait_finished(monitor, parent_id)
            snapshot = monitor.snapshot()
            children = [item for item in reversed(snapshot["jobs"]) if item.get("parent_job_id") == parent_id]
            result = {"direction": direction, "status": job["status"], "error": job["error"],
                      "elapsed_seconds": job["elapsed_seconds"],
                      "turns": [{key: child.get(key) for key in ("provider", "role", "task_mode", "write_strategy", "execution_mode", "status", "model", "reasoning_effort", "error", "latest_message")}
                                for child in children],
                      "changed_files": job.get("artifact", {}).get("changed_files", []),
                      "reply": job["latest_message"], "original_untouched_before_apply": (
                          not (workspace / "team_demo.py").exists() and (workspace / "README.md").read_text(encoding="utf-8") == original_readme)}
            if direction == "agy-to-codex" and job["status"] == "completed":
                store = ArtifactStore(workspace)
                git = shutil.which("git")
                if git:
                    patch_file = workspace / "review.patch"
                    patch_file.write_bytes(store.patch(parent_id).encode("utf-8"))
                    proc = await asyncio.to_thread(subprocess.run, [git, "apply", "--check", str(patch_file)],
                                                   cwd=workspace, capture_output=True, timeout=10)
                    result["download_patch_valid"] = proc.returncode == 0
                store.apply(parent_id)  # Only this disposable test project.
                output_file = workspace / "team_demo.py"
                code = output_file.read_text(encoding="utf-8") if output_file.is_file() else ""
                tree = ast.parse(code)
                # Validate the requested arithmetic without running generated top-level code.
                functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "add"]
                result["generated_function_verified"] = bool(
                    len(tree.body) == 1 and len(functions) == 1 and len(functions[0].body) == 1
                    and [arg.arg for arg in functions[0].args.args] == ["a", "b"]
                    and not functions[0].decorator_list and not functions[0].args.defaults
                    and isinstance(functions[0].body[0], ast.Return)
                    and isinstance(functions[0].body[0].value, ast.BinOp)
                    and isinstance(functions[0].body[0].value.op, ast.Add)
                    and isinstance(functions[0].body[0].value.left, ast.Name)
                    and isinstance(functions[0].body[0].value.right, ast.Name)
                    and functions[0].body[0].value.left.id == "a" and functions[0].body[0].value.right.id == "b")
                result["sample_apply_verified"] = store.review(parent_id)["artifact"]["applied"]
            result["verified"] = bool(job["status"] == "completed" and result["original_untouched_before_apply"]
                                      and (direction != "agy-to-codex" or result.get("generated_function_verified")
                                           and result.get("sample_apply_verified") and result.get("download_patch_valid", True)))
            return result
        finally:
            if parent_id:
                current = next((item for item in monitor.snapshot()["jobs"] if item["id"] == parent_id), None)
                if current and current["status"] not in TERMINAL:
                    monitor.cancel_job(parent_id)
            await monitor.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directions", nargs="+", choices=["agy-to-codex", "codex-to-agy"],
                        default=["agy-to-codex", "codex-to-agy"])
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    results = []
    output = PROJECT / "artifacts" / "collaboration_smoke.json"
    previous = {}
    try:
        for item in json.loads(output.read_text(encoding="utf-8")):
            if isinstance(item, dict) and item.get("direction") in ("agy-to-codex", "codex-to-agy"):
                previous[item["direction"]] = item
    except (OSError, ValueError, TypeError):
        pass
    for direction in args.directions:
        result = await check(direction, args.timeout)
        results.append(result)
        previous[direction] = result
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        (PROJECT / "artifacts").mkdir(exist_ok=True)
        output.write_text(json.dumps(list(previous.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(item["verified"] for item in results) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
