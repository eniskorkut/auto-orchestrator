#!/usr/bin/env python3
"""Real OpenCode concurrency probe.

Starts two managed sessions with a controlled five-second shell wait and
measures actual interval overlap with the same scheduler used by the runner.
This is a live provider test, not a mock or a "two calls were made" claim.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from tools.managed_exec import ManagedError, OpenCodeAdapter
    from tools.scheduler import PollScheduler
    from tools.teamwork_benchmark import concurrency_metrics
except ModuleNotFoundError:
    from managed_exec import ManagedError, OpenCodeAdapter  # type: ignore
    from scheduler import PollScheduler  # type: ignore
    from teamwork_benchmark import concurrency_metrics  # type: ignore

PROMPT = ("Use the shell tool to run the command `sleep {duration}` exactly once. "
          "Then reply with exactly: DONE. Do not edit files or do anything else.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path("/private/var/folders/sm/btb8_q_x5v71_ngxg6tkd43h0000gn/T/opencode/v74-concurrency"))
    parser.add_argument("--model", default="opencode-go/deepseek-v4.1-flash")
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--json-out", type=Path,
                        default=Path("docs/assets/v74-concurrency-probe.json"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    adapter = OpenCodeAdapter(root, model=args.model)
    scheduler = PollScheduler(max_workers=2, poll_slice=0.25)
    started = time.monotonic()
    wall_started = time.time()
    handles = {}
    starts = {}
    try:
        for task_id in ("probe-backend", "probe-frontend"):
            handles[task_id] = adapter.start(
                task_id=task_id,
                prompt=PROMPT.format(duration=f"{args.duration:g}"),
                cwd=root, title=f"v74 concurrency {task_id}", model=args.model,
            )
            starts[task_id] = handles[task_id].get("started_at") or wall_started
            scheduler.submit(task_id, handles[task_id], adapter.poll,
                             timeout=args.duration + 90)
        results = {}
        while len(results) < 2:
            for key, state in scheduler.wait(timeout=5):
                results[key] = state
        finished = time.monotonic()
    except (ManagedError, OSError, ValueError) as exc:
        print(f"CONCURRENCY PROBE ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        scheduler.close()
        for handle in handles.values():
            adapter.close(handle)
    intervals = []
    for key, state in results.items():
        intervals.append({
            "agent": key,
            "start": round(max(0.0, starts[key] - wall_started), 3),
            "end": round((state.get("finished_at") or time.time()) - wall_started, 3),
        })
    metrics = concurrency_metrics(intervals)
    wall = round(finished - started, 3)
    verified = (metrics["max_concurrent_agents"] >= 2
                and metrics["concurrent_overlap_seconds"] >= args.duration * 0.5
                and all(state.get("status") == "completed" for state in results.values()))
    payload = {
        "schema_version": 2,
        "kind": "live-opencode-managed-concurrency",
        "model": args.model,
        "opencode": "local CLI",
        "sessions": [
            {
                "task_id": key,
                "status": state.get("status"),
                "session_hash": __import__("hashlib").sha256(
                    str(state.get("session_id", "")).encode()).hexdigest()[:12],
                "tool_count": state.get("tool_count"),
                "cost": state.get("cost"),
                "tokens": state.get("tokens"),
            } for key, state in results.items()
        ],
        "wall_time_seconds": wall,
        "expected_serial_seconds": args.duration * 2,
        "metrics": metrics,
        "verified": verified,
        "note": "Real local OpenCode sessions; intervals use the same process wall clock (epoch); monotonic time is used for wall duration. Provider timestamps are not compared across hosts.",
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
