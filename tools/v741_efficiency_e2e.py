#!/usr/bin/env python3
"""V7.4.1 small-task serial baseline for adaptive-efficiency comparison."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools.managed_exec import OpenCodeAdapter
    from tools.v73_e2e import DEFAULT_MODEL, browser_env, prepare_sample
except ModuleNotFoundError:  # python tools/v741_efficiency_e2e.py
    from managed_exec import OpenCodeAdapter  # type: ignore
    from v73_e2e import DEFAULT_MODEL, browser_env, prepare_sample  # type: ignore

PROMPT = """You are one senior backend agent in a disposable repository.
Add a small backend `GET /health` endpoint that returns HTTP 200 with JSON
{"status": "ok"} and add a focused backend unit test. Create the minimal backend
package if it does not exist. Do not modify README.md or CONTRACT.md. Do not
push or deploy. Run the backend test command and report the real result.
End with a concise summary.
"""


def run(argv, cwd, env):
    started = time.monotonic()
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=900,
                            env={**os.environ, **(env or {})})
    return {
        "command": " ".join(argv),
        "exit_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": ((result.stdout or "") + (result.stderr or ""))[-2000:],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(tempfile.gettempdir()) / "opencode" / "v741-small-serial")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--json-out", type=Path,
                        default=Path("docs/assets/v741-small-serial.json"))
    args = parser.parse_args(argv)
    root = prepare_sample(args.root, reset=True)
    env = browser_env()
    adapter = OpenCodeAdapter(root, model=args.model)
    started = time.monotonic()
    handle = adapter.start(task_id="small-serial", prompt=PROMPT, cwd=root,
                           title="v741 small serial", model=args.model)
    deadline = time.monotonic() + args.max_seconds
    state = None
    try:
        while time.monotonic() < deadline:
            state = adapter.poll(handle, timeout=0)
            if state.get("status") != "running":
                break
            time.sleep(0.5)
        if state and state.get("status") == "running":
            adapter.cancel(handle)
            state = adapter.poll(handle, timeout=1)
    finally:
        adapter.close(handle)
    wall = round(time.monotonic() - started, 3)
    tests = []
    if state and state.get("status") == "completed":
        tests.append(run([sys.executable, "-m", "unittest", "discover",
                          "-s", "tests", "-q"], root, env))
    verified = bool(tests) and all(item["exit_code"] == 0 for item in tests)
    payload = {
        "schema_version": 1,
        "kind": "v741-small-task-serial-baseline",
        "label": "serial-small-task",
        "status": "passed" if verified else "failed",
        "wall_time_seconds": wall,
        "cost_usd": state.get("cost") if state else None,
        "tokens": state.get("tokens") if state else None,
        "tool_count": state.get("tool_count") if state else None,
        "session_status": state.get("status") if state else None,
        "tests": tests,
        "verified": verified,
        "note": "One real single-agent run; not a repeated statistical benchmark.",
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
