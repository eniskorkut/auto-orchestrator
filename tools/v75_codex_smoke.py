#!/usr/bin/env python3
"""V7.5 real Codex app-server smoke and managed execution evidence.

This harness uses the locally installed Codex CLI, the ChatGPT login already
present on this machine, and the official app-server JSON-RPC protocol. It
records only sanitized metadata: no raw thread IDs, prompts, assistant text,
auth tokens or local absolute paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from tools.managed_exec import CodexAdapter, ManagedError
    from tools.teamwork_runner import TeamworkRunner
except ModuleNotFoundError:
    from managed_exec import CodexAdapter, ManagedError  # type: ignore
    from teamwork_runner import TeamworkRunner  # type: ignore

DEFAULT_MODEL = "gpt-6.1-sol"
PROMPT = ("Read calc.py. Identify the likely bug and suggest one test. "
          "Do not modify any files. Reply concisely.")


def _send(process, value):
    process.stdin.write(json.dumps(value) + "\n")
    process.stdin.flush()


def _read(process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        if ready:
            line = process.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line)
            except ValueError:
                continue
    return "TIMEOUT"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _clean(value):
    text = json.dumps(value, ensure_ascii=False)
    for old, new in (
        (str(Path.home()), "$HOME"),
        (tempfile.gettempdir(), "$TMPDIR"),
        ("/private" + tempfile.gettempdir(), "$TMPDIR"),
    ):
        text = text.replace(old, new)
    return json.loads(text)


def _protocol_smoke(root: Path, model: str) -> dict:
    process = subprocess.Popen(
        ["codex", "app-server"], cwd=root, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    started = time.monotonic()
    events = []
    completed = None
    models = []
    auth_mode = None
    thread_id = None
    selected = model
    try:
        _send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"clientInfo": {
                            "name": "auto-orchestrator-v75-smoke",
                            "version": "7.5.0"},
                            "capabilities": {"experimentalApi": True}}})
        while True:
            message = _read(process)
            if message is None or message == "TIMEOUT":
                raise RuntimeError("app-server initialize timed out")
            if message.get("id") == 1:
                break
        _send(process, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "model/list",
                        "params": {}})
        while True:
            message = _read(process)
            if message is None or message == "TIMEOUT":
                raise RuntimeError("model/list timed out")
            if message.get("id") == 2:
                models = message.get("result", {}).get("data", [])
                break
        available = [item.get("id") for item in models if isinstance(item, dict)]
        if model not in available:
            selected = next((item.get("id") for item in models
                             if isinstance(item, dict) and item.get("isDefault")),
                            available[0] if available else None)
        if not selected:
            raise RuntimeError("no Codex model is available")
        _send(process, {"jsonrpc": "2.0", "id": 3, "method": "thread/start",
                        "params": {"cwd": str(root), "model": selected,
                                   "sandbox": "read-only",
                                   "approvalPolicy": "never"}})
        while True:
            message = _read(process)
            if message is None or message == "TIMEOUT":
                raise RuntimeError("thread/start timed out")
            if message.get("id") == 3:
                thread = message.get("result", {}).get("thread", {})
                thread_id = thread.get("id") if isinstance(thread, dict) else None
                break
        if not thread_id:
            raise RuntimeError("thread/start did not return a thread id")
        _send(process, {"jsonrpc": "2.0", "id": 4, "method": "turn/start",
                        "params": {"threadId": thread_id,
                                   "input": [{"type": "text", "text": PROMPT}]}})
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            message = _read(process, 5)
            if message == "TIMEOUT":
                continue
            if message is None:
                break
            events.append(message)
            method = message.get("method") if isinstance(message, dict) else None
            if method == "account/updated":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                auth_mode = params.get("authMode") or auth_mode
            if method == "turn/completed":
                completed = message
                break
        if completed is None:
            raise RuntimeError("turn/completed was not observed")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    turn = completed.get("params", {}).get("turn", {})
    tokens = None
    for message in events:
        if message.get("method") == "thread/tokenUsage/updated":
            total = (message.get("params", {}).get("tokenUsage", {})
                     .get("total", {}))
            if isinstance(total, dict):
                tokens = {
                    "input": total.get("inputTokens"),
                    "output": total.get("outputTokens"),
                    "reasoning": total.get("reasoningOutputTokens"),
                    "cache_read": total.get("cachedInputTokens"),
                    "cache_write": total.get("cacheWriteInputTokens"),
                    "total": total.get("totalTokens"),
                }
    return {
        "handshake": "initialize + initialized",
        "model_list_count": len(models),
        "selected_model": selected,
        "model_default": next((item.get("isDefault") for item in models
                               if isinstance(item, dict)
                               and item.get("id") == selected), None),
        "thread_hash": _hash(thread_id),
        "turn_status": turn.get("status"),
        "duration_ms": turn.get("durationMs"),
        "event_count": len(events),
        "completion_observed": True,
        "auth_mode": auth_mode,
        "tokens": tokens,
        "approval_events": sum(
            1 for message in events
            if "requestApproval" in str(message.get("method", ""))
        ),
        "wall_time_seconds": round(time.monotonic() - started, 3),
    }


def _adapter_smoke(root: Path, model: str) -> dict:
    adapter = CodexAdapter(root, model=model)
    started = time.monotonic()
    handle = None
    try:
        handle = adapter.start(task_id="v75-adapter", prompt=PROMPT,
                               cwd=root, title="v75 adapter smoke",
                               model=model)
        state = adapter.poll(handle, timeout=0)
        deadline = time.monotonic() + 180
        while state.get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
            state = adapter.poll(handle, timeout=0)
        return {
            "status": state.get("status"),
            "thread_hash": _hash(str(state.get("thread_id") or "")),
            "turn_status": state.get("turn_status"),
            "tool_count": state.get("tool_count"),
            "tokens": state.get("tokens"),
            "cost": state.get("cost") or None,
            "error_count": len(state.get("errors") or []),
            "completion_observed": state.get("status") == "completed",
            "wall_time_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        if handle is not None:
            adapter.close(handle)


def _teamwork_smoke(root: Path, model: str) -> dict:
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                 encoding="utf-8")
    (root / ".gitignore").write_text(".auto-orchestrator/\n", encoding="utf-8")
    for args in (["init"], ["config", "user.email", "smoke@example.invalid"],
                 ["config", "user.name", "Smoke"], ["add", "."],
                 ["commit", "-m", "initial"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                       check=True)
    plan = {
        "version": 1, "run_id": "v75-codex-managed", "max_parallel": 1,
        "pattern": "review_and_repair",
        "milestones": [{
            "id": "M1", "objective": "Read-only Codex review", "depends_on": [],
            "acceptance": ["Codex returns explicit JSON verdict"],
            "verification_policy": {"require_roles": ["reviewer"],
                                    "max_repair": 0},
        }],
        "tasks": [{
            "id": "codex-review", "role": "reviewer", "milestone": "M1",
            "phase": "verify",
            "objective": ("Review calc.py read-only. If correct, return JSON "
                          '{"verdict":"pass","findings":[],"summary":"..."} . '
                          "Do not modify files."),
            "acceptance": ["Codex returns explicit JSON verdict"],
            "read_scope": ["calc.py"], "write_scope": [], "depends_on": [],
            "timeout_seconds": 240, "max_retries": 0,
            "verify_commands": [
                f'"{sys.executable}" -c "print(\'review-ok\')"'
            ],
            "department": "review", "provider": "codex", "model": model,
            "sandbox": "read-only", "approval_policy": "never",
        }],
    }
    runner = TeamworkRunner(root, plan, model=model, max_seconds=300)
    try:
        summary = runner.run()
    finally:
        runner.close()
    task = summary.get("milestones", [{}])[0].get("tasks", [{}])[0]
    return {
        "status": summary.get("status"),
        "milestone_status": summary.get("milestones", [{}])[0].get("status"),
        "task_state": task.get("state"),
        "verdict": (task.get("result") or {}).get("verdict"),
        "managed": (task.get("result") or {}).get("managed"),
        "completion_observed": summary.get("status") == "passed",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--phase", choices=["all", "protocol", "adapter", "teamwork"],
                        default="all")
    parser.add_argument("--json-out", type=Path,
                        default=Path("docs/assets/v75-codex-smoke.json"))
    args = parser.parse_args(argv)
    codex = shutil.which("codex")
    if not codex:
        print("CODEX SMOKE ERROR: codex binary is not installed", file=sys.stderr)
        return 2
    version = subprocess.run([codex, "--version"], capture_output=True,
                             text=True).stdout.strip()
    if args.phase != "all":
        root = Path(tempfile.mkdtemp(prefix=f"v75-codex-{args.phase}-"))
        (root / "calc.py").write_text("def add(a, b):\n    return a - b\n",
                                      encoding="utf-8")
        if args.phase == "protocol":
            result = _protocol_smoke(root, args.model)
        elif args.phase == "adapter":
            result = _adapter_smoke(root, args.model)
        else:
            result = _teamwork_smoke(root, args.model)
        text = json.dumps(_clean({"phase": args.phase, "result": result}),
                          indent=2, ensure_ascii=False) + "\n"
        print(text, end="")
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text, encoding="utf-8")
        return 0 if result.get("completion_observed") else 1
    login_result = subprocess.run([codex, "login", "status"], capture_output=True,
                                  text=True)
    login = ((login_result.stdout or "") + (login_result.stderr or "")).lower()
    root = Path(tempfile.mkdtemp(prefix="v75-codex-"))
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n",
                                  encoding="utf-8")
    # Run the full Teamwork task first. Codex app-server hooks/MCP startup can
    # leave transient child state that delays a second app-server turn.
    teamwork_root = Path(tempfile.mkdtemp(prefix="v75-codex-teamwork-"))
    teamwork = _teamwork_smoke(teamwork_root, args.model)
    protocol = _protocol_smoke(root, args.model)
    adapter = _adapter_smoke(root, protocol["selected_model"])
    if isinstance(teamwork.get("managed"), dict) and not teamwork["managed"].get("cost"):
        teamwork["managed"]["cost"] = None
    payload = _clean({
        "schema_version": 1,
        "kind": "v75-codex-app-server-smoke",
        "codex_version": version.split()[-1] if version else None,
        "auth_mode": (protocol.get("auth_mode")
                      or ("chatgpt" if "chatgpt" in login else "unknown")),
        "model_requested": args.model,
        "protocol": protocol,
        "managed_adapter": adapter,
        "managed_teamwork": teamwork,
        "native_cancel_ack": False,
        "limitations": [
            "Single local sample; no repeated latency benchmark.",
            "Cost is not reported by the app-server notification stream.",
            "Native Codex cancellation acknowledgement is not claimed.",
            "This is Codex CLI/app-server managed execution, not an IDE-native subagent.",
        ],
    })
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if (protocol["completion_observed"]
                 and adapter["completion_observed"]
                 and teamwork["completion_observed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
