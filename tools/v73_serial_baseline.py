#!/usr/bin/env python3
"""Run one real serial single-agent baseline for the V7.3 sample.

The result is measured evidence only. It is not a human time-saving claim and
may be faster or slower than the orchestrated run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from tools.managed_exec import ManagedError, OpenCodeAdapter
    from tools.v73_e2e import CONTRACT, browser_env, git
except ModuleNotFoundError:  # python tools/v73_serial_baseline.py
    from managed_exec import ManagedError, OpenCodeAdapter  # type: ignore
    from v73_e2e import CONTRACT, browser_env, git  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
MODEL = "opencode-go/deepseek-v4.1-flash"


def prepare(root: Path) -> Path:
    root = Path(root).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "README.md").write_text(
        "# teamwork-serial-sample\n\nDisposable single-agent baseline.\n", encoding="utf-8")
    (root / "CONTRACT.md").write_text(CONTRACT, encoding="utf-8")
    (root / ".gitignore").write_text(
        ".auto-orchestrator/\n__pycache__/\n*.pyc\nnode_modules/\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.email", "baseline@example.invalid")
    git(root, "config", "user.name", "Serial Baseline")
    git(root, "add", ".")
    git(root, "commit", "-m", "chore: serial baseline scaffold")
    return root


def prompt() -> str:
    return """You are a single senior full-stack agent working in a disposable repository.
Implement the complete task-manager project defined in CONTRACT.md in this one session.

Requirements:
- Python 3.11 stdlib backend in backend/, static frontend in frontend/,
  backend unit tests, and a CommonJS browser E2E in e2e/check.cjs.
- No third-party runtime packages. Use only the documented API.
- Create all files, run the backend tests and the browser E2E, fix failures,
  and leave a clean working tree.
- Playwright is available through NODE_PATH and AO_CHROMIUM_EXECUTABLE.
- Do not modify README.md or CONTRACT.md. Do not push or deploy.
- End with a concise summary of commands and results.
"""


def run_command(root: Path, argv: list[str], env: dict) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1800,
                                env={**os.environ, **env})
        return {
            "name": " ".join(argv), "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "output": ((result.stdout or "") + (result.stderr or ""))[-4000:],
        }
    except subprocess.TimeoutExpired:
        return {"name": " ".join(argv), "status": "failed", "exit_code": None,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "output": "timeout"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path("/private/var/folders/sm/btb8_q_x5v71_ngxg6tkd43h0000gn/T/opencode/teamwork-serial-sample"))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)
    root = prepare(args.root)
    env = browser_env()
    adapter = OpenCodeAdapter(root, model=args.model)
    started = time.time()
    handle = None
    try:
        handle = adapter.start(
            task_id="serial-baseline", prompt=prompt(), cwd=root,
            title="teamwork serial baseline", model=args.model,
        )
        deadline = time.monotonic() + args.max_seconds
        state = adapter.poll(handle, timeout=0)
        while state.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
            state = adapter.poll(handle, timeout=0)
        if state.get("status") == "running":
            adapter.cancel(handle)
            state = adapter.poll(handle, timeout=1)
    except (ManagedError, OSError, ValueError) as exc:
        print(f"SERIAL BASELINE ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if handle is not None:
            adapter.close(handle)
    wall = round(time.time() - started, 3)

    tests = []
    if state.get("status") == "completed":
        tests.append(run_command(root, [sys.executable, "-m", "unittest",
                                        "discover", "-s", "tests", "-v"], env))
        tests.append(run_command(root, ["node", "e2e/check.cjs"], env))
    verified = bool(tests) and all(item["status"] == "passed" for item in tests)
    payload = {
        "label": "serial-single-agent",
        "mode": "serial",
        "wall_time_seconds": wall,
        "verified": verified,
        "verified_tasks": 8 if verified else 0,
        "total_tasks": 8,
        "tests_passed": sum(item["status"] == "passed" for item in tests),
        "tests_total": len(tests),
        "repairs": 0,
        "retries": 0,
        "cost_usd": state.get("cost") or 0,
        "agent_intervals": [{"agent": "serial", "start": 0, "end": wall}],
        "model": args.model,
        "hardware": "local macOS arm64",
        "cache": "provider cache warm; no controlled cache normalization",
        "acceptance": [
            "backend API and validation", "frontend UI", "backend unit suite",
            "browser E2E", "config base", "create/list/toggle", "error path", "clean tree",
        ],
        "managed": {
            "status": state.get("status"), "session_id": state.get("session_id"),
            "tool_count": state.get("tool_count"), "tokens": state.get("tokens"),
            "text": (state.get("text") or "")[-4000:],
        },
        "verification_commands": tests,
        "root": str(root),
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
