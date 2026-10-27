#!/usr/bin/env python3
"""V7.8 live cross-IDE handoff E2E for the IDEs available on this machine.

Unlike ``tools/v78_handoff_e2e.py`` (simulated surfaces, always runnable),
this script executes the real Codex CLI and the real OpenCode CLI in a
throw-away git project, with our ownership/handoff CLI in between:

    Codex live write -> native plan -> handoff -> OpenCode live write
    -> handoff back -> Codex live write

The second IDE must recover the ``next_action`` marker from ``ide status`` and
write it with its own native file tool. If a CLI is missing, unauthenticated or
times out, the result is reported as ``unavailable``/``failed`` for that
platform; it is never recorded as passed.

Usage:
    python3.11 tools/v78_live_handoff_e2e.py \\
        --json-out docs/assets/v78-live-handoff.json [--require-live]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "tools" / "universal_teamwork.py"
CHAT = REPO / "tools" / "teamwork_chat.py"


class LiveFailure(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise LiveFailure(message)


def _env(platform):
    env = dict(os.environ)
    env["AO_PLATFORM"] = platform
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _tail(text, limit=1200):
    text = (text or "").strip()
    return text[-limit:]


def run_cli(root: Path, platform: str, *args, expect: int = 0,
            timeout: int = 180) -> dict:
    command = [sys.executable, str(CLI), "--root", str(root),
               "--platform", platform, *map(str, args)]
    started = time.time()
    process = subprocess.run(command, capture_output=True, text=True,
                             timeout=timeout, env=_env(platform), cwd=str(root))
    payload = None
    if process.stdout.strip():
        try:
            payload = json.loads(process.stdout)
        except ValueError:
            payload = None
    if expect == 0 and process.returncode != 0:
        raise LiveFailure(f"{platform} {' '.join(map(str, args))} failed: "
                          f"{_tail(process.stderr, 400)}")
    return {"payload": payload, "code": process.returncode,
            "seconds": round(time.time() - started, 3),
            "stderr": _tail(process.stderr)}


def run_agent(command, cwd: Path, *, timeout: int, env=None) -> dict:
    started = time.time()
    environment = dict(env or os.environ)
    # Child tools may resolve the project from $PWD instead of the process
    # cwd; keep both consistent so a live agent cannot write into the repo
    # that happened to launch the test.
    environment["PWD"] = str(cwd)
    try:
        process = subprocess.run(command, cwd=str(cwd), capture_output=True,
                                 text=True, timeout=timeout,
                                 env=environment)
        return {
            "code": process.returncode,
            "seconds": round(time.time() - started, 3),
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "code": None,
            "seconds": round(time.time() - started, 3),
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout after {timeout}s",
        }


def run_agent_verified(command, cwd: Path, *, timeout: int, env, verify,
                       attempts: int = 2) -> tuple[dict, dict, list]:
    """Run a live agent and verify its observable side effect.

    Live models are nondeterministic; a bounded retry is recorded, never
    hidden. A missing file is still a failure after the last attempt.
    """
    records = []
    last: LiveFailure | None = None
    for index in range(attempts):
        result = run_agent(command, cwd, timeout=timeout, env=env)
        try:
            if result["code"] != 0:
                raise LiveFailure(
                    f"agent exited {result['code']}: "
                    f"{_tail(result['stderr'], 300)}")
            detail = verify(result)
        except LiveFailure as exc:
            last = exc
            records.append({
                "attempt": index + 1, "ok": False,
                "seconds": result["seconds"], "exit": result["code"],
                "error": str(exc)[:500],
            })
            continue
        records.append({
            "attempt": index + 1, "ok": True,
            "seconds": result["seconds"], "exit": result["code"],
        })
        return result, detail, records
    raise LiveFailure(f"{last} (after {attempts} attempts)")


def new_project(base: Path) -> Path:
    root = base / "proje canlı"
    root.mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"),
                 ("config", "user.email", "live@example.invalid"),
                 ("config", "user.name", "V78 Live")):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)
    (root / ".gitignore").write_text(".auto-orchestrator/\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# Live handoff project\n\nTwo IDEs continue one task.\n",
        encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"],
                   check=True, capture_output=True)
    return root


def request_file(root: Path, text: str) -> str:
    command = [sys.executable, str(CHAT), "--root", str(root), "request-path"]
    process = subprocess.run(command, capture_output=True, text=True,
                             timeout=60, env=_env("codex"), cwd=str(root))
    if process.returncode != 0:
        raise LiveFailure(f"request-path failed: {_tail(process.stderr, 300)}")
    value = json.loads(process.stdout)
    target = Path(value["absolute_path"])
    target.write_text(text, encoding="utf-8")
    return str(target)


def marker(prefix: str) -> str:
    return f"{prefix}-{os.urandom(4).hex().upper()}"


def ownership(root: Path) -> dict:
    return json.loads(
        (root / ".auto-orchestrator" / "ide" / "ownership.json").read_text(
            encoding="utf-8"))


def _tool_version(binary: str) -> str | None:
    try:
        process = subprocess.run([binary, "--version"], capture_output=True,
                                 text=True, timeout=30)
        return _tail(process.stdout or process.stderr, 120) or None
    except (OSError, subprocess.SubprocessError):
        return None


def _codex_tokens(output: str) -> int | None:
    match = re.search(r"tokens used\s*\n\s*([0-9][0-9,.]*)", output)
    if not match:
        return None
    try:
        return int(float(match.group(1).replace(",", "")))
    except ValueError:
        return None


def _opencode_model(output: str) -> str | None:
    match = re.search(r">\s*[a-z-]+\s*·\s*([A-Za-z0-9._/-]+)", output)
    return match.group(1) if match else None


def scenario(base: Path, *, timeout: int, steps: list) -> dict:

    def record(name, fn):
        at = time.time()
        value = fn()
        steps.append({"name": name, "ok": True,
                      "seconds": round(time.time() - at, 3)})
        return value

    root = new_project(base)
    codex_bin = shutil.which("codex")
    opencode_bin = shutil.which("opencode")
    check(codex_bin is not None, "codex CLI is not installed")
    check(opencode_bin is not None, "opencode CLI is not installed")

    codex_version = _tool_version(codex_bin)
    opencode_version = _tool_version(opencode_bin)

    def codex_activate():
        return run_cli(root, "codex", "ide", "activate")["payload"]

    record("codex takes ownership", codex_activate)

    relative = request_file(root, (
        "Create a small README section that documents the live handoff "
        "fixture. Keep the change documentation-only."))

    def plan():
        payload = run_cli(root, "codex", "plan", "--request-file", relative,
                          "--run-id", "run-live-1", "--grill-mode",
                          "off")["payload"]
        check(payload.get("status") == "plan_ready", "live plan was not written")
        return payload

    record("codex writes a native plan", plan)

    record("user approves the plan", lambda: run_cli(
        root, "codex", "approve-plan", "--run-id", "run-live-1",
        "--approver", "live-e2e")["payload"])
    record("codex starts the plan natively", lambda: run_cli(
        root, "codex", "start", "--plan", "run-live-1")["payload"])

    part_one = marker("CODEX-PART")
    prompt_one = (
        "You are continuing a project in Codex. Do not start another IDE.\n"
        f"Create the file {root / 'codex_part.txt'} whose entire content is "
        f"exactly: {part_one}\n"
        "Do not modify any other file. Do not commit."
    )

    def codex_write():
        def verify(result):
            target = root / "codex_part.txt"
            check(target.is_file(),
                  "codex did not create codex_part.txt; output: "
                  + _tail(result["stdout"] + result["stderr"], 700))
            content = target.read_text(encoding="utf-8").strip()
            check(content == part_one,
                  f"codex_part.txt has unexpected content: {content[:200]!r}")
            return {"tokens_reported": _codex_tokens(
                result["stdout"] + result["stderr"]),
                "file": "codex_part.txt"}

        result, detail, attempts = run_agent_verified(
            [codex_bin, "exec", "--sandbox", "workspace-write",
             "--skip-git-repo-check", "-C", str(root), prompt_one],
            root, timeout=timeout, env=_env("codex"), verify=verify)
        return {"exit": result["code"], "seconds": result["seconds"],
                "attempts": attempts, **detail}

    codex_first = record("codex writes natively", codex_write)

    next_action = marker("NEXT")
    handoff_one = {}

    def handoff_to_opencode():
        prepared = run_cli(root, "codex", "ide", "prepare", "--target-platform",
                           "opencode", "--allow-dirty", "--summary",
                           "Codex finished its part; continue the README task",
                           "--next-action",
                           f"Write the handoff marker {next_action} to "
                           "opencode_part.txt with your own file tool")["payload"]
        handoff = (prepared.get("result") or {}).get("handoff") or {}
        run_cli(root, "codex", "ide", "release", "--handoff",
                handoff["handoff_id"])
        accepted = run_cli(root, "opencode", "ide", "accept", "--handoff",
                           handoff["handoff_id"], "--digest",
                           handoff["digest"])["payload"]
        check(accepted.get("ok") is True, "opencode could not accept the handoff")
        handoff_one.update({"handoff_id": handoff["handoff_id"],
                            "digest": handoff["digest"]})
        return accepted

    record("codex hands ownership to opencode", handoff_to_opencode)

    prompt_two = (
        "You are continuing this project in OpenCode after a sequential IDE "
        "handoff. The previous IDE's chat is not available; use project "
        "memory only. Do not start Codex or another IDE.\n"
        "Run this read-only command and read the `next_action` value from "
        "`result.snapshot`:\n"
        f"  {sys.executable} {CLI} --root {root} --platform opencode ide status\n"
        f"Then create the file {root / 'opencode_part.txt'} whose entire "
        "content is exactly that next_action value (a single line, no "
        "quotes).\n"
        "Do not modify any other file. Do not commit."
    )

    def opencode_write():
        status = run_cli(root, "opencode", "ide", "status")["payload"]
        snapshot = ((status.get("result") or {}).get("snapshot") or {})
        check(snapshot.get("next_action") == (
            f"Write the handoff marker {next_action} to opencode_part.txt "
            "with your own file tool"),
            "ide status did not expose the next action after the handoff")

        def verify(result):
            target = root / "opencode_part.txt"
            check(target.is_file(),
                  "opencode did not create opencode_part.txt; output: "
                  + _tail(result["stdout"] + result["stderr"], 700))
            content = target.read_text(encoding="utf-8").strip()
            check(next_action in content,
                  "opencode did not recover the next action from the handoff; "
                  f"content={content[:300]!r}; output: "
                  + _tail(result["stdout"] + result["stderr"], 500))
            return {"model_reported": _opencode_model(
                result["stdout"] + result["stderr"]),
                "file": "opencode_part.txt"}

        result, detail, attempts = run_agent_verified(
            [opencode_bin, "run", "--auto", "--standalone", prompt_two],
            root, timeout=timeout, env=_env("opencode"), verify=verify)
        return {"exit": result["code"], "seconds": result["seconds"],
                "attempts": attempts, **detail}

    opencode_step = record("opencode recovers the next action and writes", opencode_write)

    final_action = marker("FINAL")
    handoff_two = {}

    def handoff_back():
        prepared = run_cli(root, "opencode", "ide", "prepare",
                           "--target-platform", "codex", "--allow-dirty",
                           "--summary",
                           "OpenCode continued the task; finish in Codex",
                           "--next-action",
                           f"Write the final marker {final_action} to "
                           "codex_final.txt with your own file tool")["payload"]
        handoff = (prepared.get("result") or {}).get("handoff") or {}
        run_cli(root, "opencode", "ide", "release", "--handoff",
                handoff["handoff_id"])
        accepted = run_cli(root, "codex", "ide", "accept", "--handoff",
                           handoff["handoff_id"], "--digest",
                           handoff["digest"])["payload"]
        check(accepted.get("ok") is True, "codex could not accept the handoff")
        handoff_two.update({"handoff_id": handoff["handoff_id"],
                            "digest": handoff["digest"]})
        return accepted

    record("opencode hands ownership back to codex", handoff_back)

    prompt_three = (
        "You are continuing this project in Codex after a second sequential "
        "IDE handoff. Do not start OpenCode or another IDE.\n"
        "Run this read-only command and read `result.snapshot.next_action`:\n"
        f"  {sys.executable} {CLI} --root {root} --platform codex ide status\n"
        f"Then create the file {root / 'codex_final.txt'} whose entire "
        "content is exactly that next_action value (a single line, no "
        "quotes).\n"
        "Do not modify any other file. Do not commit."
    )

    def codex_final():
        status = run_cli(root, "codex", "ide", "status")["payload"]
        snapshot = ((status.get("result") or {}).get("snapshot") or {})
        check(snapshot.get("next_action") == (
            f"Write the final marker {final_action} to codex_final.txt "
            "with your own file tool"),
            "ide status did not expose the final next action")

        def verify(result):
            target = root / "codex_final.txt"
            check(target.is_file(),
                  "codex did not create codex_final.txt; output: "
                  + _tail(result["stdout"] + result["stderr"], 700))
            content = target.read_text(encoding="utf-8").strip()
            check(final_action in content,
                  "codex did not recover the final next action; "
                  f"content={content[:300]!r}")
            return {"tokens_reported": _codex_tokens(
                result["stdout"] + result["stderr"]),
                "file": "codex_final.txt"}

        result, detail, attempts = run_agent_verified(
            [codex_bin, "exec", "--sandbox", "workspace-write",
             "--skip-git-repo-check", "-C", str(root), prompt_three],
            root, timeout=timeout, env=_env("codex"), verify=verify)
        return {"exit": result["code"], "seconds": result["seconds"],
                "attempts": attempts, **detail}

    codex_last = record("codex continues from the second handoff", codex_final)

    record_ = ownership(root)
    check(record_["state"] == "transferred" and record_["owner"]["ide"] == "codex",
          "ownership did not end with codex")
    check(record_["previous_owner"]["ide"] == "opencode",
          "previous owner is not opencode")
    check(record_["native_cancel_ack"] is False and record_["stop_ack"] is False,
          "live handoff fabricated a stop acknowledgement")
    check(not (root / ".auto-orchestrator" / "teamwork" / "active.json").exists(),
          "live native flow wrote a managed runner record")
    return {
        "root": root,
        "steps": steps,
        "codex_version": codex_version,
        "opencode_version": opencode_version,
        "first_handoff": handoff_one,
        "second_handoff": handoff_two,
        "codex_first": codex_first,
        "opencode": opencode_step,
        "codex_last": codex_last,
        "next_actions": {"opencode": next_action, "codex": final_action},
    }


def sanitize(value, replacements):
    if isinstance(value, str):
        for needle, label in replacements:
            if needle:
                value = value.replace(needle, label)
        return value
    if isinstance(value, list):
        return [sanitize(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item, replacements) for key, item in value.items()}
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--require-live", action="store_true",
                        help="exit non-zero when a live surface is unavailable")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="ao-v78-live-"))
    started = time.time()
    available = {
        "codex": shutil.which("codex"),
        "opencode": shutil.which("opencode"),
        "cursor": shutil.which("cursor-agent") or shutil.which("cursor"),
        "antigravity": shutil.which("antigravity"),
    }
    result = None
    error = None
    steps: list = []
    try:
        result = scenario(base, timeout=args.timeout, steps=steps)
    except LiveFailure as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - harness boundary
        error = f"{type(exc).__name__}: {exc}"

    live_ides = []
    if result is not None:
        live_ides = ["codex", "opencode"]
    payload = {
        "schema_version": 1,
        "kind": "auto-orchestrator-v78-live-handoff-e2e",
        "version": "7.8",
        "mode": "live",
        "generated_at": time.time(),
        "python": sys.version.split()[0],
        "os": sys.platform,
        "project": "$PROJECT",
        "available_cli": {key: bool(value) for key, value in available.items()},
        "live_ides": live_ides,
        "unavailable": {
            key: ("no documented CLI agent entry" if not value else None)
            for key, value in available.items()
            if key not in live_ides
        },
        "status": "passed" if result is not None else "failed",
        "error": error,
        "steps": steps,
        "evidence": {
            key: value for key, value in (result or {}).items()
            if key not in ("root", "steps")
        },
        "duration_seconds": round(time.time() - started, 3),
        "limits": [
            "Codex and OpenCode were executed through their own CLIs in a "
            "temporary project; Cursor and Antigravity had no documented CLI "
            "agent entry and are not claimed.",
            "The prompts are scripted test prompts, not a benchmark of model "
            "quality or human time saved.",
        ],
    }
    replacements = [(str(base), "$PROJECT"), (str(Path.home()), "$HOME")]
    payload = sanitize(payload, replacements)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(text, end="")
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    if result is None:
        return 1 if args.require_live else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
