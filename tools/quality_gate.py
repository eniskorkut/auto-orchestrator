#!/usr/bin/env python3
"""Opt-in, fail-closed evidence quality gate for a single OpenCode run.

Reads metadata only. It never executes a project command or interprets test output.
"""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path

class GateError(ValueError):
    pass

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TOOLS = {"bash", "shell", "powershell"}
MAX_BYTES = 16384

def load_policy(root: Path):
    path = Path(root) / ".auto-orchestrator" / "verification.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise GateError("Invalid or symlinked verification policy")
    try:
        raw = path.read_bytes()
        policy = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError("Invalid verification policy JSON") from exc
    validate_policy(policy)
    policy["_sha256"] = hashlib.sha256(raw).hexdigest()
    return policy

def validate_policy(policy):
    if not isinstance(policy, dict) or policy.get("version") != 1 or policy.get("auto_verify") is not True:
        raise GateError("Auto-verification requires an explicit version 1 opt-in policy")
    checks = policy.get("required_checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
        raise GateError("Policy requires 1..16 checks")
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            raise GateError("Check must be an object")
        name, tool, command = check.get("id"), check.get("tool"), check.get("command")
        if not isinstance(name, str) or not ID.fullmatch(name) or name in seen:
            raise GateError("Invalid or duplicate check ID")
        if tool not in TOOLS or not isinstance(command, str) or not command or len(command) > 500 or "\n" in command or "\r" in command:
            raise GateError("Check needs an exact supported native tool and command")
        seen.add(name)
    return policy

def assess(events, policy) -> dict:
    """Required checks must have successful *native* exit codes after last observed write."""
    if policy is None:
        return {"verified": False, "reason": "No explicit verification policy"}
    validate_policy(policy)
    if not events or events[0].get("event") != "start":
        return {"verified": False, "reason": "No run start"}
    end = next((i for i, e in enumerate(events) if e.get("event") == "finish"), None)
    if end is None or events[end].get("status") != "success":
        return {"verified": False, "reason": "Run not successfully completed"}
    if any(e.get("event") == "agent-start" for e in events[:end]) and (
        len([e for e in events[:end] if e.get("event") == "agent-start"]) !=
        len([e for e in events[:end] if e.get("event") == "agent-stop"])):
        return {"verified": False, "reason": "Unmatched native agent span"}
    last_change = max((i for i, e in enumerate(events[:end]) if e.get("event") in ("mutation", "agent-stop")), default=-1)
    good = set()
    for e in events[last_change + 1:end]:
        if e.get("event") == "check" and e.get("source") == "native-tool" and (not policy.get("_sha256") or e.get("policy_hash") == policy["_sha256"]) and isinstance(e.get("exit_code"), int) and not isinstance(e.get("exit_code"), bool):
            if e["exit_code"] == 0:
                good.add(e.get("check_id"))
            else:
                good.discard(e.get("check_id"))
    required = {x["id"] for x in policy["required_checks"]}
    missing = sorted(required - good)
    if missing:
        return {"verified": False, "reason": "Missing or failed checks: " + ", ".join(missing)}
    if any(e.get("event") == "mutation" for e in events[end+1:]):
        return {"verified": False, "reason": "Mutation after finish"}
    return {"verified": True, "reason": "All configured native checks passed", "check_ids": sorted(required)}
