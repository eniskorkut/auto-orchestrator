#!/usr/bin/env python3
"""V7.4.1 single-chat shell-security E2E.

Starts a real run through the bridge using a request file containing quotes,
backticks, command substitution, semicolons, newlines and Unicode. The request
must reach the plan unchanged; no shell marker may be created. The run is then
cancelled locally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools import teamwork_chat
    from tools.v73_e2e import DEFAULT_MODEL, prepare_sample
except ModuleNotFoundError:  # python tools/v741_chat_security_e2e.py
    import teamwork_chat  # type: ignore
    from v73_e2e import DEFAULT_MODEL, prepare_sample  # type: ignore


def _args(root: Path, run_id=None, request_file=None):
    return type("Args", (), {"root": str(root), "run_id": run_id,
                             "request": None, "request_file": request_file,
                             "request_stdin": False, "mode": "fast",
                             "model": DEFAULT_MODEL, "max_seconds": 3600})()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(tempfile.gettempdir()) / "opencode" / "v741-chat-security")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--run-id", default="v741-chat-security")
    parser.add_argument("--json-out", type=Path,
                        default=Path("docs/assets/v741-chat-security.json"))
    args = parser.parse_args(argv)
    root = prepare_sample(args.root, reset=True)
    marker = root / "SHELL_MARKER_MUST_NOT_EXIST"
    request = (
        "Add a backend GET /health endpoint with a unit test.\n"
        "Security probe (must remain text, never execute): "
        "single=' double=\" backtick=`id` substitution=$(touch "
        f"{marker}) ; echo pwned\n"
        "Markdown/JSON: ```json\n{\"note\": \"ĞüşiÖÇ — safe\"}\n```\n"
        "Path-like text: ../../etc/passwd and ; rm -rf / are inert."
    )
    request_dir = root / ".auto-orchestrator" / "teamwork" / "requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / "request-security.md"
    request_path.write_text(request, encoding="utf-8")
    start_args = _args(root, args.run_id, request_file=str(request_path))
    started = time.monotonic()
    start_result = teamwork_chat.start(start_args)
    plan_path = Path(start_result["plan"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    request_preserved = plan.get("request") == request
    statuses = []
    active = None
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            status = teamwork_chat.status(_args(root, args.run_id))
            if status["status"] not in statuses:
                statuses.append(status["status"])
            active = status
            if status["owner_connected"] and status["active_tasks"]:
                break
            if status["status"] in ("failed", "cancelled"):
                break
            time.sleep(0.5)
        cancel_result = teamwork_chat.cancel(_args(root, args.run_id))
        deadline = time.monotonic() + 60
        final = None
        while time.monotonic() < deadline:
            final = teamwork_chat.status(_args(root, args.run_id))
            if final["status"] in ("failed", "cancelled", "passed"):
                break
            time.sleep(0.5)
    finally:
        pass
    marker_exists = marker.exists()
    payload = {
        "schema_version": 1,
        "kind": "v741-single-chat-shell-security",
        "run_id": start_result["run_id"],
        "mode": start_result["mode"],
        "status": final.get("status") if final else None,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "request": {
            "sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
            "characters": len(request),
            "preview": request.replace(str(marker), "$MARKER")[:600],
            "preserved_in_plan": request_preserved,
        },
        "shell_marker_created": marker_exists,
        "owner_verified": bool(active and active.get("owner_verified")),
        "owner_connected": bool(active and active.get("owner_connected")),
        "status_transitions": statuses,
        "active_tasks_observed": active.get("active_tasks", []) if active else [],
        "cancel": cancel_result,
        "final_status": final,
        "native_cancel_ack": False,
        "local_signal_only": True,
        "note": (
            "Real bridge run with a validated request file. The request text was "
            "never inserted into a shell command. Cancellation is a local signal."
        ),
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    passed = request_preserved and not marker_exists and bool(active and active.get("owner_verified"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
