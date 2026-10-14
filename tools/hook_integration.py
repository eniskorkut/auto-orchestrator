"""Conservative, reversible integration into existing Codex and Antigravity hooks.json.

Only appends our exact hook groups or owned top-level Antigravity namespace.
Never rewrites or removes unrelated user hook definitions.
"""
from __future__ import annotations

import json
from copy import deepcopy

class HookIntegrationError(ValueError):
    pass

def parse_hooks(content: str) -> dict:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HookIntegrationError("Existing hooks.json is not valid JSON; preserve and integrate manually") from exc
    if not isinstance(data, dict):
        raise HookIntegrationError("Existing hooks.json must be a JSON object")
    return data

def merge(existing: dict, fragment: dict, platform: str) -> tuple[dict, bool]:
    data = deepcopy(existing)
    if platform == "codex":
        base = data.setdefault("hooks", {})
        if not isinstance(base, dict) or not isinstance(fragment.get("hooks"), dict):
            raise HookIntegrationError("Codex hooks must map event names to arrays")
        changed = False
        for event, groups in fragment["hooks"].items():
            if event not in base:
                base[event] = []
            if not isinstance(base[event], list):
                raise HookIntegrationError(f"Existing Codex {event} hooks are not an array")
            for group in groups:
                if group not in base[event]:
                    base[event].append(deepcopy(group))
                    changed = True
        return data, changed
    if platform == "antigravity":
        name = "auto-orchestrator"
        desired = fragment[name]
        if name in data and data[name] != desired:
            raise HookIntegrationError("Conflicting auto-orchestrator Antigravity hook; do not overwrite")
        if name not in data:
            data[name] = deepcopy(desired)
            return data, True
        return data, False
    raise HookIntegrationError("Unknown hook platform")

def remove(existing: dict, fragment: dict, platform: str) -> tuple[dict, bool, bool]:
    """Returns result, changed, remaining_managed_items (e.g. user modified)."""
    data = deepcopy(existing)
    if platform == "codex":
        base = data.get("hooks", {})
        if not isinstance(base, dict):
            return data, False, True
        changed = False
        for event, groups in fragment["hooks"].items():
            current = base.get(event)
            if not isinstance(current, list):
                continue
            for group in groups:
                if group in current:
                    current.remove(group)
                    changed = True
            if not current:
                del base[event]
        if not base and "hooks" in data:
            del data["hooks"]
        return data, changed, False
    if platform == "antigravity":
        name = "auto-orchestrator"
        if data.get(name) == fragment[name]:
            del data[name]
            return data, True, False
        return data, False, name in data
    raise HookIntegrationError("Unknown hook platform")

def pretty(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
