#!/usr/bin/env python3
"""Compare measured serial vs orchestrated runs without inventing savings.

The benchmark accepts recorded measurements only. It separates:

* ``measured_wall_time`` — start/end timestamps recorded by the runner;
* ``observed_overlap`` — sum of verified agent interval overlaps;
* ``verified_improvement`` — a wall-time ratio only when both runs passed their
  own acceptance checks and were measured on comparable inputs.

It never estimates human time saved and never turns a missing measurement into
zero or a success.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_INTERVALS = 200
SCHEMA_VERSION = 2


class BenchmarkError(ValueError):
    pass


def _number(value, field: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{field} must be a number")
    number = float(value)
    if not number >= minimum:
        raise BenchmarkError(f"{field} must be >= {minimum}")
    return number


def _integer(value, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BenchmarkError(f"{field} must be an integer >= {minimum}")
    return value


def _optional_metric(value, field: str):
    if value is None:
        return None
    return _number(value, field)


def _text(value, field: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BenchmarkError(f"{field} must be a non-empty string")
    return value.strip()


def normalize_run(value: dict) -> dict:
    if not isinstance(value, dict):
        raise BenchmarkError("run must be an object")
    mode = value.get("mode")
    if mode not in ("serial", "parallel"):
        raise BenchmarkError("mode must be serial or parallel")
    intervals = value.get("agent_intervals", [])
    if not isinstance(intervals, list) or len(intervals) > MAX_INTERVALS:
        raise BenchmarkError("agent_intervals must be a bounded array")
    normalized_intervals = []
    for index, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            raise BenchmarkError(f"agent_intervals[{index}] must be an object")
        start = _number(interval.get("start"), f"agent_intervals[{index}].start")
        end = _number(interval.get("end"), f"agent_intervals[{index}].end")
        if end < start:
            raise BenchmarkError(f"agent_intervals[{index}] ends before it starts")
        normalized_intervals.append({
            "agent": _text(interval.get("agent", "agent"), f"agent_intervals[{index}].agent", 80),
            "start": start, "end": end,
        })
    task_graph = value.get("tasks", [])
    if not isinstance(task_graph, list) or len(task_graph) > 500:
        raise BenchmarkError("tasks must be a bounded array")
    normalized_tasks = []
    for index, item in enumerate(task_graph):
        if not isinstance(item, dict):
            raise BenchmarkError(f"tasks[{index}] must be an object")
        task_id = _text(item.get("id"), f"tasks[{index}].id", 80)
        deps = item.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise BenchmarkError(f"tasks[{index}].depends_on must be strings")
        duration = _number(item.get("duration_seconds", 0),
                           f"tasks[{index}].duration_seconds")
        normalized_tasks.append({"id": task_id, "depends_on": deps,
                                 "duration_seconds": duration})
    verified = value.get("verified")
    if not isinstance(verified, bool):
        raise BenchmarkError("verified must be a boolean")
    return {
        "label": _text(value.get("label", mode), "label", 80),
        "mode": mode,
        "wall_time_seconds": _number(value.get("wall_time_seconds"), "wall_time_seconds"),
        "queue_wait_seconds": _number(value.get("queue_wait_seconds", 0), "queue_wait_seconds"),
        "verified": verified,
        "verified_tasks": _integer(value.get("verified_tasks", 0), "verified_tasks"),
        "total_tasks": _integer(value.get("total_tasks", 0), "total_tasks"),
        "tests_passed": _integer(value.get("tests_passed", 0), "tests_passed"),
        "tests_total": _integer(value.get("tests_total", 0), "tests_total"),
        "repairs": _integer(value.get("repairs", 0), "repairs"),
        "retries": _integer(value.get("retries", 0), "retries"),
        "cost_usd": (_number(value.get("cost_usd"), "cost_usd")
                     if value.get("cost_usd") is not None else None),
        "cost_measured": value.get("cost_usd") is not None,
        "agent_intervals": normalized_intervals,
        "critical_path_seconds": _optional_metric(
            value.get("critical_path_seconds"), "critical_path_seconds"),
        "scheduler_idle_seconds": _optional_metric(
            value.get("scheduler_idle_seconds"), "scheduler_idle_seconds"),
        "dispatch_latency_ms": _optional_metric(
            value.get("dispatch_latency_ms"), "dispatch_latency_ms"),
        "verification_overhead_seconds": _optional_metric(
            value.get("verification_overhead_seconds"), "verification_overhead_seconds"),
        "repair_overhead_seconds": _optional_metric(
            value.get("repair_overhead_seconds"), "repair_overhead_seconds"),
        "model": _text(value.get("model", "unknown"), "model", 120),
        "hardware": _text(value.get("hardware", "unrecorded"), "hardware", 200),
        "cache": _text(value.get("cache", "unrecorded"), "cache", 120),
        "acceptance": [_text(item, "acceptance", 300)
                       for item in value.get("acceptance", [])][:50],
        "tasks": normalized_tasks,
    }


def overlap_seconds(intervals: list[dict]) -> float:
    """Observed union length of agent intervals (legacy name).

    This is *not* concurrent overlap: it counts every instant at least one agent
    was active. Use :func:`concurrency_metrics` for true overlap.
    """
    return concurrency_metrics(intervals)["agent_span_union_seconds"]


def concurrency_metrics(intervals: list[dict]) -> dict:
    if not intervals:
        return {
            "agent_span_union_seconds": 0.0,
            "concurrent_overlap_seconds": 0.0,
            "max_concurrent_agents": 0,
            "average_concurrency": 0.0,
            "agent_seconds": 0.0,
        }
    events = []
    for item in intervals:
        events.append((item["start"], 1))
        events.append((item["end"], -1))
    # End events sort before start events at the same instant: touching
    # intervals are not concurrent overlap.
    events.sort(key=lambda pair: (pair[0], pair[1]))
    active = 0
    overlap = 0.0
    union = 0.0
    last = events[0][0]
    max_active = 0
    for timestamp, delta in events:
        width = timestamp - last
        if width > 0 and active >= 1:
            union += width
        if width > 0 and active >= 2:
            overlap += width
        active += delta
        max_active = max(max_active, active)
        last = timestamp
    agent_seconds = sum(item["end"] - item["start"] for item in intervals)
    return {
        "agent_span_union_seconds": round(union, 3),
        "concurrent_overlap_seconds": round(overlap, 3),
        "max_concurrent_agents": max_active,
        "average_concurrency": round(agent_seconds / union, 3) if union > 0 else 0.0,
        "agent_seconds": round(agent_seconds, 3),
    }


def critical_path_seconds(tasks: list[dict]) -> float | None:
    if not tasks:
        return None
    by_id = {task["id"]: task for task in tasks}
    if len(by_id) != len(tasks):
        raise BenchmarkError("task ids must be unique")
    memo = {}
    visiting = set()

    def visit(task_id):
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            raise BenchmarkError("task graph contains a cycle")
        if task_id not in by_id:
            raise BenchmarkError("task dependency is unknown")
        visiting.add(task_id)
        dependency_max = max((visit(dep) for dep in by_id[task_id]["depends_on"]),
                             default=0.0)
        visiting.remove(task_id)
        memo[task_id] = dependency_max + by_id[task_id]["duration_seconds"]
        return memo[task_id]

    return round(max(visit(task_id) for task_id in by_id), 3)


def compare(serial: dict, parallel: dict) -> dict:
    left = normalize_run(serial)
    right = normalize_run(parallel)
    serial_conc = concurrency_metrics(left["agent_intervals"])
    parallel_conc = concurrency_metrics(right["agent_intervals"])
    comparable = (
        left["verified"] and right["verified"]
        and left["total_tasks"] == right["total_tasks"]
        and left["tests_total"] == right["tests_total"]
        and left["wall_time_seconds"] > 0 and right["wall_time_seconds"] > 0
    )
    improvement = None
    improvement_reason = None
    if comparable:
        improvement = round(left["wall_time_seconds"] - right["wall_time_seconds"], 3)
        improvement_reason = "measured wall-time difference for comparable, independently verified runs"
    elif not left["verified"] or not right["verified"]:
        improvement_reason = "both runs must be independently verified before claiming improvement"
    elif left["total_tasks"] != right["total_tasks"] or left["tests_total"] != right["tests_total"]:
        improvement_reason = "task or test counts differ; runs are not comparable"
    else:
        improvement_reason = "wall time is missing or zero"
    return {
        "version": SCHEMA_VERSION,
        "metrics_schema": {
            "version": SCHEMA_VERSION,
            "agent_span_union_seconds": "union of all agent-active instants",
            "concurrent_overlap_seconds": "seconds with at least two agents active",
            "max_concurrent_agents": "observed peak concurrency",
            "average_concurrency": "agent-seconds divided by union span",
        },
        "serial": left,
        "parallel": right,
        "observed": {
            "serial": serial_conc,
            "parallel": parallel_conc,
            "parallel_minus_serial_span_seconds": round(
                parallel_conc["agent_span_union_seconds"]
                - serial_conc["agent_span_union_seconds"], 3),
        },
        "measured_wall_time": {
            "serial_seconds": left["wall_time_seconds"],
            "parallel_seconds": right["wall_time_seconds"],
            "difference_seconds": round(left["wall_time_seconds"] - right["wall_time_seconds"], 3),
        },
        "verified_improvement": {
            "comparable": comparable,
            "wall_time_difference_seconds": improvement,
            "reason": improvement_reason,
            "human_time_saved_claim": False,
        },
        "cost": {
            "serial_usd": left["cost_usd"],
            "parallel_usd": right["cost_usd"],
            "difference_usd": (round(left["cost_usd"] - right["cost_usd"], 6)
                               if left["cost_usd"] is not None
                               and right["cost_usd"] is not None else None),
            "measured": bool(left["cost_measured"] and right["cost_measured"]),
        },
        "critical_path": {
            "serial_seconds": critical_path_seconds(left["tasks"]),
            "parallel_seconds": critical_path_seconds(right["tasks"]),
            "source": "declared task DAG and measured durations; null when omitted",
        },
        "quality": {
            "serial_verified_tasks": left["verified_tasks"],
            "parallel_verified_tasks": right["verified_tasks"],
            "serial_test_pass_ratio": (left["tests_passed"] / left["tests_total"]
                                       if left["tests_total"] else None),
            "parallel_test_pass_ratio": (right["tests_passed"] / right["tests_total"]
                                         if right["tests_total"] else None),
            "serial_repairs": left["repairs"],
            "parallel_repairs": right["repairs"],
        },
        "disclaimer": ("Measured values only. This is not human time saved and not a "
                       "general performance guarantee."),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", type=Path, required=True)
    parser.add_argument("--parallel", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        left = json.loads(args.serial.read_text(encoding="utf-8"))
        right = json.loads(args.parallel.read_text(encoding="utf-8"))
        result = compare(left, right)
    except (OSError, ValueError, BenchmarkError) as exc:
        print(f"BENCHMARK ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Serial: {result['serial']['wall_time_seconds']}s, "
              f"parallel: {result['parallel']['wall_time_seconds']}s")
        print(f"Concurrent overlap: serial={result['observed']['serial']['concurrent_overlap_seconds']}s, "
              f"parallel={result['observed']['parallel']['concurrent_overlap_seconds']}s")
        print(f"Verified improvement: {result['verified_improvement']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
