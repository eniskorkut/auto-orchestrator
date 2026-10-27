#!/usr/bin/env python3
"""V7.8 sequential IDE handoff end-to-end harness (simulated native surfaces).

Every step drives the real CLI (`tools/universal_teamwork.py`) in a separate
process against a throw-away git project. The IDE surfaces are simulated: no
Codex/OpenCode/Cursor/Antigravity agent is started and no model is called.
Evidence is therefore labelled ``simulated``; live runs use
``tools/v78_live_handoff_e2e.py``.

Usage:
    python3.11 tools/v78_handoff_e2e.py \\
        --json-out docs/assets/v78-handoff-sim-e2e.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "tools" / "universal_teamwork.py"
CHAT = REPO / "tools" / "teamwork_chat.py"
PLATFORMS = ("codex", "opencode", "cursor", "antigravity")
BENIGN_REQUEST = (
    "Add a short contributor note to the README and one small unit test.\n"
    "Keep the change documentation-only and do not touch runtime code."
)
AUTH_REQUEST = (
    "Kullanıcı girişi, ödeme akışı ve veri saklama politikası ekle. "
    "JWT token yenileme, kart saklama ve silme davranışı tanımlansın."
)


class ScenarioFailure(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise ScenarioFailure(message)


# -- process helpers -----------------------------------------------------
def _env(platform):
    env = dict(os.environ)
    env["AO_PLATFORM"] = platform
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_cli(root: Path, platform: str, *args, expect: int = 0,
            timeout: int = 180):
    command = [sys.executable, str(CLI), "--root", str(root),
               "--platform", platform, *map(str, args)]
    started = time.time()
    process = subprocess.run(command, capture_output=True, text=True,
                             timeout=timeout, env=_env(platform), cwd=str(REPO))
    elapsed = round(time.time() - started, 3)
    payload = None
    if process.stdout.strip():
        try:
            payload = json.loads(process.stdout)
        except ValueError:
            payload = None
    if expect == 0:
        if process.returncode != 0:
            raise ScenarioFailure(
                f"{platform} {' '.join(map(str, args))} failed with exit "
                f"{process.returncode}: {process.stderr.strip()[:400]}")
        if payload is None:
            raise ScenarioFailure(
                f"{platform} {' '.join(map(str, args))} returned no JSON: "
                f"{process.stdout.strip()[:300]}")
    else:
        if process.returncode == 0:
            raise ScenarioFailure(
                f"{platform} {' '.join(map(str, args))} was expected to fail "
                f"but exited 0: {json.dumps(payload)[:300]}")
    return {"payload": payload, "stdout": process.stdout,
            "stderr": process.stderr, "code": process.returncode,
            "seconds": elapsed}


def run_chat(root: Path, *args) -> dict:
    command = [sys.executable, str(CHAT), "--root", str(root), *map(str, args)]
    process = subprocess.run(command, capture_output=True, text=True,
                             timeout=120, env=_env("codex"), cwd=str(REPO))
    if process.returncode != 0:
        raise ScenarioFailure(f"teamwork_chat {' '.join(args)} failed: "
                              f"{process.stderr.strip()[:300]}")
    return json.loads(process.stdout)


def git(root: Path, *args) -> str:
    process = subprocess.run(["git", "-C", str(root), *args],
                             capture_output=True, text=True, timeout=60)
    if process.returncode != 0:
        raise ScenarioFailure(f"git {' '.join(args)} failed: "
                              f"{process.stderr.strip()[:300]}")
    return process.stdout


def request_file(root: Path, text: str) -> str:
    value = run_chat(root, "request-path")
    target = Path(value["absolute_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    # The bridge resolves --request-file against the caller's cwd, so the
    # harness passes the absolute path while the CLI still verifies that it
    # lives inside .auto-orchestrator/teamwork/requests.
    return str(target)


def new_project(base: Path, name: str) -> Path:
    root = base / name
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "e2e@example.invalid")
    git(root, "config", "user.name", "V78 E2E")
    (root / ".gitignore").write_text(
        ".auto-orchestrator/\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# Demo project\n\nSequential IDE handoff fixture.\n", encoding="utf-8")
    git(root, "add", ".gitignore", "README.md")
    git(root, "commit", "-qm", "init")
    return root


def ownership(root: Path) -> dict:
    return json.loads(
        (root / ".auto-orchestrator" / "ide" / "ownership.json").read_text(
            encoding="utf-8"))


def snapshot(root: Path, handoff_id: str) -> dict:
    path = root / ".auto-orchestrator" / "ide" / "handoffs" / f"{handoff_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_and_accept(root: Path, source: str, target: str, *, next_action=None,
                       allow_dirty=False) -> dict:
    args = ["ide", "prepare", "--target-platform", target,
            "--summary", f"handoff {source} to {target}"]
    if next_action:
        args += ["--next-action", next_action]
    if allow_dirty:
        args.append("--allow-dirty")
    prepared = run_cli(root, source, *args)["payload"]
    handoff = (prepared.get("result") or {}).get("handoff") or {}
    handoff_id = handoff.get("handoff_id")
    digest = handoff.get("digest")
    check(bool(handoff_id and digest), "prepare did not return a handoff id/digest")
    released = run_cli(root, source, "ide", "release",
                       "--handoff", handoff_id)["payload"]
    check(released.get("ok") is True, "release was not ok")
    accepted = run_cli(root, target, "ide", "accept", "--handoff", handoff_id,
                       "--digest", digest)
    payload = accepted["payload"]
    check(payload.get("ok") is True,
          f"accept failed: {_reason_text(payload)}")
    return {"handoff_id": handoff_id, "digest": digest,
            "prepared": prepared, "released": released, "accepted": payload}


def _reason_text(payload) -> str:
    reasons = ((payload or {}).get("result") or {}).get("reasons") or []
    return "; ".join(item.get("message") or item.get("code") or str(item)
                     for item in reasons) or json.dumps(payload)[:300]


# -- scenarios -----------------------------------------------------------
def scenario_sequential(base: Path) -> dict:
    root = new_project(base, "sequential")
    started = time.time()
    steps = []

    def step(name, fn):
        at = time.time()
        value = fn()
        steps.append({"name": name, "ok": True,
                      "seconds": round(time.time() - at, 3)})
        return value

    activated = step("codex activates ownership", lambda: run_cli(
        root, "codex", "ide", "activate")["payload"])
    check(activated.get("ok") is True and activated.get("state") == "active",
          "codex activation failed")

    relative = request_file(root, BENIGN_REQUEST)

    def plan():
        payload = run_cli(root, "codex", "plan", "--request-file", relative,
                          "--run-id", "run-seq-1", "--grill-mode", "off")["payload"]
        check(payload.get("status") == "plan_ready", "plan was not created")
        check((payload.get("approval") or {}).get("status") == "pending",
              "plan must start with a pending approval")
        check(payload.get("execution") == "native", "plan is not native")
        return payload

    plan_payload = step("codex writes an inspectable native plan", plan)

    def approve():
        payload = run_cli(root, "codex", "approve-plan", "--run-id",
                          "run-seq-1", "--approver", "e2e-user")["payload"]
        check(payload.get("status") == "approved",
              "plan approval was not recorded")
        plan = json.loads((root / ".auto-orchestrator" / "plans" /
                           "run-seq-1.json").read_text(encoding="utf-8"))
        check((plan.get("approval") or {}).get("status") == "approved",
              "plan artifact does not carry the approval")
        check((plan.get("approval") or {}).get("plan_sha256") ==
              payload.get("plan_sha256"),
              "approval digest does not match the plan")
        return payload

    step("user approves the plan explicitly", approve)

    def start():
        payload = run_cli(root, "codex", "start", "--plan", "run-seq-1")["payload"]
        check(payload.get("status") == "native_plan_ready",
              "native plan did not start")
        check("pid" not in payload, "native start exposed a process id")
        return payload

    step("codex starts the approved plan natively", start)
    check(not (root / ".auto-orchestrator" / "teamwork" / "active.json").exists(),
          "native start wrote a managed runner record")

    handoff = step("codex prepares a handoff for opencode",
                   lambda: prepare_and_accept(
                       root, "codex", "opencode",
                       next_action="Finish the README note and run the test"))
    record = ownership(root)
    check(record["state"] == "transferred", "ownership is not transferred")
    check(record["owner"]["ide"] == "opencode", "opencode is not the owner")
    check(record["previous_owner"]["ide"] == "codex",
          "previous owner was not preserved")
    check(record["native_cancel_ack"] is False and record["stop_ack"] is False,
          "handoff fabricated a stop acknowledgement")

    def foreign_read():
        payload = run_cli(root, "cursor", "context")["payload"]
        owners = payload.get("ownership") or {}
        check(owners.get("foreign") is True,
              "read-only context did not report a foreign owner")
        return payload

    step("cursor inspects context read-only while opencode owns", foreign_read)

    def foreign_write():
        result = run_cli(root, "cursor", "ide", "activate", expect=1)
        message = (result["stderr"] or "").lower()
        check("opencode" in message or "ownership" in message,
              f"foreign activation error was not explained: {message[:200]}")
        return result

    step("cursor cannot take ownership from a live opencode owner", foreign_write)

    def continue_plan():
        payload = run_cli(root, "opencode", "start", "--plan",
                          "run-seq-1")["payload"]
        check(payload.get("status") == "native_plan_ready",
              "opencode could not continue the approved native plan")
        check(payload.get("plan_authored_by") == "codex",
              "plan authorship was not preserved")
        check(payload.get("handoff_continuation") is True,
              "continuation after handoff was not reported")
        return payload

    step("opencode continues the same approved plan", continue_plan)

    snapshot_value = snapshot(root, handoff["handoff_id"])
    check(snapshot_value["source_ide"] == "codex"
          and snapshot_value["target_ide"] == "opencode",
          "snapshot source/target mismatch")
    check(snapshot_value["run_id"] == "run-seq-1",
          "snapshot lost the run id")
    check(snapshot_value["next_action"],
          "snapshot lost the next action")
    check(snapshot_value["digest"] == handoff["digest"],
          "snapshot digest mismatch")

    def back_to_codex():
        prepare_and_accept(root, "opencode", "codex",
                           next_action="Continue verification in Codex")
        final = ownership(root)
        check(final["owner"]["ide"] == "codex", "codex did not regain ownership")
        check(final["previous_owner"]["ide"] == "opencode",
              "previous owner was not opencode")
        return final

    step("opencode hands ownership back to codex", back_to_codex)
    return {"root": root, "steps": steps,
            "seconds": round(time.time() - started, 3)}


def scenario_grill_switch(base: Path) -> dict:
    root = new_project(base, "grill-switch")
    started = time.time()
    steps = []

    def step(name, fn):
        at = time.time()
        value = fn()
        steps.append({"name": name, "ok": True,
                      "seconds": round(time.time() - at, 3)})
        return value

    run_cli(root, "codex", "ide", "activate")
    relative = request_file(root, AUTH_REQUEST)

    def start():
        payload = run_cli(root, "codex", "grill", "start", "--request-file",
                          relative, "--grill-mode", "auto")["payload"]
        check(payload.get("question"), "grill start returned no question")
        return payload

    started_interview = step("codex starts a Grill-Me interview", start)
    interview_id = started_interview["question"]["interview_id"]

    answered = []

    def answer_two():
        question = started_interview["question"]
        for _ in range(2):
            option = (question.get("options") or [{}])[0].get("id")
            payload = run_cli(root, "codex", "grill", "answer",
                              "--question-id", question["question_id"],
                              "--option", str(option))["payload"]
            answered.append(question["question_key"])
            question = payload.get("next")
            check(question, "interview ended earlier than expected")
            question["interview_id"] = interview_id
        return question

    open_question = step("user answers two questions", answer_two)
    open_question_id = open_question["question_id"]

    def switch():
        prepare_and_accept(root, "codex", "opencode",
                           next_action="Continue the interview at the open question")
        resumed = run_cli(root, "opencode", "grill", "resume")["payload"]
        question = resumed.get("question") or {}
        check(question.get("question_id") == open_question_id,
              "opencode did not resume the exact open question")
        check(question.get("interview_id") == interview_id,
              "interview id changed across the IDE switch")
        check((resumed.get("interview") or {}).get("origin_platform") == "codex",
              "interview origin platform was not preserved")
        keys = {item["question_key"] for item in
                (resumed.get("interview") or {}).get("questions", [])}
        check(not (set(answered) & {question.get("question_key")}),
              "an answered question was asked again")
        return resumed

    step("opencode resumes the interview at the open question", switch)

    def finish():
        remaining = 16
        while remaining:
            remaining -= 1
            payload = run_cli(root, "opencode", "grill", "resume")["payload"]
            question = payload.get("question")
            if not question:
                break
            option = (question.get("options") or [{}])[0].get("id")
            run_cli(root, "opencode", "grill", "answer",
                    "--question-id", question["question_id"],
                    "--option", str(option))
        approved = run_cli(root, "opencode", "grill", "approve")["payload"]
        contract = approved.get("contract") or approved.get("requirements_contract")
        check(contract, "approval did not create a requirements contract")
        return approved

    approved = step("opencode finishes and approves the interview", finish)
    record = ownership(root)
    check(record["owner"]["ide"] == "opencode", "opencode ownership was lost")

    def back():
        prepare_and_accept(root, "opencode", "codex",
                           next_action="Implement the approved contract")
        summary = run_cli(root, "codex", "grill", "summary",
                          "--interview-id", interview_id)["payload"]
        check(summary.get("interview_id") == interview_id,
              "interview id changed after the second switch")
        check(summary.get("status") == "approved",
              "approval was not preserved across the switch")
        check(not summary.get("blocking_questions"),
              "approved interview still reports blocking questions")
        return summary

    step("approval and interview survive the switch back", back)
    return {"root": root, "steps": steps,
            "seconds": round(time.time() - started, 3),
            "interview_id": interview_id,
            "contract_id": (approved.get("contract") or {}).get("contract_id")}


def scenario_race(base: Path) -> dict:
    root = new_project(base, "race")
    started = time.time()
    processes = []
    for platform in PLATFORMS:
        command = [sys.executable, str(CLI), "--root", str(root),
                   "--platform", platform, "ide", "activate"]
        processes.append((platform, subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_env(platform), cwd=str(REPO))))
    results = []
    for platform, process in processes:
        stdout, stderr = process.communicate(timeout=180)
        results.append({"platform": platform, "code": process.returncode,
                        "stdout": stdout[-2000:], "stderr": stderr[-2000:]})
    winners = [item for item in results if item["code"] == 0]
    losers = [item for item in results if item["code"] != 0]
    check(len(winners) == 1,
          f"expected exactly one winner, got {[w['platform'] for w in winners]}")
    for loser in losers:
        message = (loser["stderr"] or "").lower()
        check("ownership" in message or "held" in message
              or "owner" in message,
              f"loser {loser['platform']} failed for an unclear reason: "
              f"{message[:200]}")
    record = ownership(root)
    check(record["owner"]["ide"] == winners[0]["platform"],
          "recorded owner is not the race winner")
    check(record["fence"] == 1, "ownership fence advanced more than once")
    return {"root": root, "seconds": round(time.time() - started, 3),
            "winner": winners[0]["platform"],
            "losers": [item["platform"] for item in losers],
            "steps": [{"name": "four concurrent ide activate calls",
                       "ok": True}]}


def scenario_dirty(base: Path) -> dict:
    root = new_project(base, "dirty")
    started = time.time()
    run_cli(root, "codex", "ide", "activate")
    (root / "README.md").write_text(
        "# Demo project\n\nEdited but not committed.\n", encoding="utf-8")
    (root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    prepared = run_cli(root, "codex", "ide", "prepare", "--target-platform",
                       "opencode", "--allow-dirty", "--next-action",
                       "Commit the edited README and finish feature.py")["payload"]
    handoff = (prepared.get("result") or {}).get("handoff") or {}
    check(handoff.get("handoff_id"), "dirty prepare returned no handoff")
    value = snapshot(root, handoff["handoff_id"])
    check(value["dirty"] is True, "snapshot does not record a dirty worktree")
    paths = {item["path"] for item in value["changed_files"]}
    check({"README.md", "feature.py"} <= paths,
          f"changed files missing from snapshot: {sorted(paths)}")
    run_cli(root, "codex", "ide", "release", "--handoff", handoff["handoff_id"])
    accepted = run_cli(root, "opencode", "ide", "accept", "--handoff",
                       handoff["handoff_id"], "--digest",
                       handoff["digest"])["payload"]
    check(accepted.get("ok") is True,
          f"dirty accept failed: {_reason_text(accepted)}")
    check(ownership(root)["owner"]["ide"] == "opencode",
          "dirty worktree transfer did not move ownership")
    return {"root": root, "seconds": round(time.time() - started, 3),
            "changed_files": sorted(paths),
            "steps": [{"name": "dirty worktree transferred with allow_dirty",
                       "ok": True}]}


def scenario_drift(base: Path) -> dict:
    root = new_project(base, "drift")
    started = time.time()
    run_cli(root, "codex", "ide", "activate")
    prepared = run_cli(root, "codex", "ide", "prepare", "--target-platform",
                       "opencode", "--summary", "drift test")["payload"]
    handoff = (prepared.get("result") or {}).get("handoff") or {}
    run_cli(root, "codex", "ide", "release", "--handoff",
            handoff["handoff_id"])
    (root / "drift.txt").write_text("changed after handoff\n", encoding="utf-8")
    git(root, "add", "drift.txt")
    git(root, "commit", "-qm", "drift after handoff")
    # The CLI must refuse the accept with a non-zero exit and a precise reason.
    attempt = run_cli(root, "opencode", "ide", "accept", "--handoff",
                      handoff["handoff_id"], "--digest", handoff["digest"],
                      expect=1)
    message = (attempt["stderr"] or "").lower()
    check("head" in message or "branch" in message or "worktree" in message,
          f"drift refusal was unclear: {message[:200]}")
    evidence = base / "drift-recovery.json"
    evidence.write_text(json.dumps({"kind": "operator",
                                    "ref": "manual:drift"}), encoding="utf-8")
    recovered = run_cli(root, "cursor", "ide", "recover", "--reason",
                        "source committed after handoff", "--evidence",
                        str(evidence), "--apply")["payload"]
    check(recovered.get("ok") is True, "recovery after drift was refused")
    check(ownership(root).get("handoff") is None,
          "recovery left the stale handoff in place")
    activated = run_cli(root, "cursor", "ide", "activate")["payload"]
    check(activated.get("ok") is True, "cursor could not activate after recovery")
    return {"root": root, "seconds": round(time.time() - started, 3),
            "steps": [
                {"name": "accept refuses HEAD drift", "ok": True},
                {"name": "explicit recovery clears the stale handoff", "ok": True},
            ]}


def scenario_recovery(base: Path) -> dict:
    root = new_project(base, "recovery")
    started = time.time()
    activated = run_cli(root, "codex", "ide", "activate")["payload"]
    token = (activated.get("result") or {}).get("owner_token")
    check(token, "activation returned no owner token")
    path = root / ".auto-orchestrator" / "ide" / "ownership.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["owner"]["heartbeat_at"] = time.time() - record["owner"]["ttl_seconds"] - 300
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    status = run_cli(root, "codex", "ide", "status")["payload"]
    liveness = status.get("result", {}).get("liveness") or status.get("liveness") or {}
    owner = (liveness.get("owner") or {})
    check(owner.get("status") == "unknown",
          f"stale heartbeat must be unknown, got {owner.get('status')}")
    check(owner.get("expired") is True, "stale heartbeat was not reported expired")

    takeover = run_cli(root, "opencode", "ide", "activate", expect=1)
    check("codex" in (takeover["stderr"] or "").lower(),
          "takeover refusal did not name the current owner")

    refused = run_cli(root, "cursor", "ide", "recover", "--apply", expect=1)
    check("evidence" in (refused["stderr"] or "").lower(),
          "recovery without evidence was not refused")

    evidence = base / "recovery-evidence.json"
    evidence.write_text(json.dumps({"kind": "operator",
                                    "ref": "manual:crash"}), encoding="utf-8")
    recovered = run_cli(root, "cursor", "ide", "recover", "--reason",
                        "codex crashed", "--evidence", str(evidence),
                        "--apply")["payload"]
    check(recovered.get("ok") is True, "explicit recovery was refused")
    record = ownership(root)
    transition = record.get("last_transition") or {}
    check(transition.get("stop_ack") is False,
          "recovery fabricated a stop acknowledgement")
    check(record.get("native_cancel_ack") is False,
          "recovery fabricated a native cancel acknowledgement")
    check(transition.get("liveness") == "unknown",
          "recovery claimed a verified liveness")
    activated = run_cli(root, "cursor", "ide", "activate")["payload"]
    check(activated.get("ok") is True, "cursor activation after recovery failed")
    return {"root": root, "seconds": round(time.time() - started, 3),
            "steps": [
                {"name": "stale heartbeat is unknown, not dead", "ok": True},
                {"name": "automatic takeover is refused", "ok": True},
                {"name": "recovery without evidence is refused", "ok": True},
                {"name": "explicit recovery records unknown liveness", "ok": True},
            ]}


def scenario_no_foreign_process(base: Path) -> dict:
    root = new_project(base, "native")
    started = time.time()
    run_cli(root, "codex", "ide", "activate")
    relative = request_file(root, BENIGN_REQUEST)
    run_cli(root, "codex", "plan", "--request-file", relative, "--run-id",
            "run-native-1", "--grill-mode", "off")
    run_cli(root, "codex", "approve-plan", "--run-id", "run-native-1",
            "--approver", "e2e-user")
    started_plan = run_cli(root, "codex", "start", "--plan",
                           "run-native-1")["payload"]
    check("pid" not in started_plan, "native start returned a PID")
    teamwork_dir = root / ".auto-orchestrator" / "teamwork"
    check(not (teamwork_dir / "active.json").exists(),
          "native start wrote active.json (managed runner record)")
    check(not list(teamwork_dir.glob("runner-*.lock")),
          "native start created a managed runner lock")
    plan_path = root / ".auto-orchestrator" / "plans" / "run-native-1.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    check(plan.get("execution_mode") == "native",
          "plan execution mode is not native")
    check(all(task.get("provider") == "codex" for task in plan.get("tasks", [])),
          "native plan tasks reference a foreign provider")
    task = (plan.get("tasks") or [{}])[0]
    tests = base / "native-tests.json"
    tests.write_text(json.dumps([{"name": "unit", "status": "passed",
                                  "exit_code": 0}]), encoding="utf-8")
    report = run_cli(root, "codex", "native-report", "--run-id", "run-native-1",
                     "--task-id", task["id"], "--status", "passed",
                     "--verdict", "passed", "--tests", str(tests))["payload"]
    check(report.get("verified") is False,
          "native self-report was counted as verified")
    check(report.get("completion_recorded") is False,
          "native self-report recorded completion")
    check(report.get("native_session_id") is None,
          "native report invented a session id")
    return {"root": root, "seconds": round(time.time() - started, 3),
            "task_id": task["id"],
            "steps": [
                {"name": "native start spawns no provider or runner", "ok": True},
                {"name": "native self-report is evidence, not verification",
                 "ok": True},
            ]}


def _bridge_scenario(base: Path, name: str, first: str, second: str) -> dict:
    """Two-way handoff between two IDE surfaces that run no managed agent."""
    root = new_project(base, name)
    started = time.time()
    run_cli(root, first, "ide", "activate")
    forward = prepare_and_accept(root, first, second,
                                 next_action=f"Continue the task in {second}")
    value = snapshot(root, forward["handoff_id"])
    check(value["source_ide"] == first and value["target_ide"] == second,
          "snapshot did not record the source/target IDE")
    status = run_cli(root, second, "ide", "status")["payload"]
    check(status.get("active") is True, f"{second} is not active after accept")
    check(status.get("owner_platform") == second,
          f"{second} is not the recorded owner")
    back = prepare_and_accept(root, second, first,
                              next_action=f"Continue the task in {first}")
    record = ownership(root)
    check(record["owner"]["ide"] == first, f"{first} did not regain ownership")
    check(record["previous_owner"]["ide"] == second,
          "previous owner was not preserved on the way back")
    check(record["native_cancel_ack"] is False and record["stop_ack"] is False,
          "handoff fabricated a stop acknowledgement")
    return {
        "root": root,
        "seconds": round(time.time() - started, 3),
        "handoffs": [forward["handoff_id"], back["handoff_id"]],
        "steps": [
            {"name": f"{first} -> {second} ownership transfer", "ok": True},
            {"name": f"{second} -> {first} ownership transfer", "ok": True},
        ],
    }


def scenario_opencode_cursor(base: Path) -> dict:
    return _bridge_scenario(base, "opencode-cursor", "opencode", "cursor")


def scenario_codex_antigravity(base: Path) -> dict:
    return _bridge_scenario(base, "codex-antigravity", "codex", "antigravity")


SCENARIOS = {
    "sequential-codex-opencode-codex": scenario_sequential,
    "grill-me-mid-switch": scenario_grill_switch,
    "ownership-race": scenario_race,
    "dirty-worktree-transfer": scenario_dirty,
    "head-drift-refusal": scenario_drift,
    "crash-recovery": scenario_recovery,
    "no-foreign-process": scenario_no_foreign_process,
    "opencode-cursor-opencode": scenario_opencode_cursor,
    "codex-antigravity-codex": scenario_codex_antigravity,
}


# -- reporting -----------------------------------------------------------
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
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary projects for inspection")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    selected = args.only or list(SCENARIOS)
    base = Path(tempfile.mkdtemp(prefix="ao-v78-e2e-"))
    results = []
    started = time.time()
    for name in selected:
        scenario_started = time.time()
        try:
            outcome = SCENARIOS[name](base)
            results.append({
                "id": name, "ok": True,
                "seconds": round(time.time() - scenario_started, 3),
                "steps": outcome.get("steps", []),
                "detail": {key: value for key, value in outcome.items()
                           if key not in ("root", "steps", "seconds")},
            })
        except ScenarioFailure as exc:
            results.append({
                "id": name, "ok": False,
                "seconds": round(time.time() - scenario_started, 3),
                "error": str(exc), "steps": [],
            })
        except Exception as exc:  # noqa: BLE001 - harness boundary
            results.append({
                "id": name, "ok": False,
                "seconds": round(time.time() - scenario_started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-2000:], "steps": [],
            })
    duration = round(time.time() - started, 3)
    failed = [item for item in results if not item["ok"]]
    payload = {
        "schema_version": 1,
        "kind": "auto-orchestrator-v78-handoff-e2e",
        "version": "7.8",
        "mode": "simulated",
        "live_ides": [],
        "native_surfaces": {
            platform: "simulated" for platform in PLATFORMS
        },
        "seed": args.seed,
        "generated_at": time.time(),
        "python": sys.version.split()[0],
        "os": sys.platform,
        "project": "$PROJECT",
        "scenarios": results,
        "summary": {
            "total": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "duration_seconds": duration,
        },
        "notes": [
            "No model, provider or IDE process was started.",
            "Files under .auto-orchestrator/ are excluded from the dirty digest.",
            "Live IDE evidence is recorded separately and never merged here.",
        ],
    }
    replacements = [
        (str(base), "$PROJECT"),
        (str(Path.home()), "$HOME"),
    ]
    payload = sanitize(payload, replacements)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(text, end="")
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
