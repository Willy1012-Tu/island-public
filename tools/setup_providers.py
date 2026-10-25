"""Inspect/login installed CLIs and optionally install the official agy locally."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from cli_providers import discover_providers

MANIFEST = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/windows_amd64.json"


def install_antigravity() -> None:
    with urllib.request.urlopen(MANIFEST, timeout=30) as response:
        manifest = json.load(response)
    url, expected = manifest["url"], manifest["sha512"]
    if not url.startswith("https://storage.googleapis.com/antigravity-public/antigravity-cli/"):
        raise ValueError("Unexpected official download host/path.")
    if len(expected) != 128 or any(c not in "0123456789abcdefABCDEF" for c in expected):
        raise ValueError("Invalid manifest checksum.")
    target = PROJECT / ".tools" / "agy.exe"
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_suffix(".download")
    digest = hashlib.sha512()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest().lower(), expected.lower()):
            raise ValueError("Downloaded CLI checksum does not match its official manifest.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"provider": "antigravity", "version": manifest["version"],
                      "path": str(target), "sha512_verified": True}, ensure_ascii=False))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Connect native CLI account logins without API keys.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    login = sub.add_parser("login")
    login.add_argument("provider", choices=("codex", "claude", "antigravity"))
    sub.add_parser("install-antigravity")
    args = parser.parse_args()
    if args.action == "install-antigravity":
        install_antigravity()
        return 0
    providers = discover_providers()
    if args.action == "status":
        print(json.dumps(providers, ensure_ascii=False, indent=2))
        return 0
    selected = next(provider for provider in providers if provider["id"] == args.provider)
    if not selected["available"]:
        print(selected["reason"], file=sys.stderr)
        return 1
    command = [selected["command"]]
    if args.provider == "codex":
        command += ["login"]
    else:
        print("Claude 4.6 與 Antigravity 辦公室共用 agy 帳號。請完成 Google 帳號登入後離開 CLI，回到監控台。")
    # Run interactively in the user's terminal; never capture login credentials.
    return subprocess.call(command, cwd=PROJECT, shell=False)


if __name__ == "__main__":
    raise SystemExit(main())
