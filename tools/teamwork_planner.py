#!/usr/bin/env python3
"""Deterministic template planner for the V7.3 teamwork engine.

``plan_request`` turns a short request into a rich milestone plan. It makes no
LLM call, spawns no agent and reads no repository. Every acceptance criterion
and verification command is written explicitly; every template assumption that
needs the user's real repository is marked in ``repo_inputs`` and on the
affected task.

The output validates with both the existing project planner and
``tools.teamwork.validate_teamwork_plan``.
"""
from __future__ import annotations

import re

try:
    from tools.supervisor import SupervisorError, identifier
    from tools.teamwork import PATTERNS, validate_teamwork_plan
except ModuleNotFoundError:  # Running as ``python tools/teamwork_planner.py``.
    from supervisor import SupervisorError, identifier  # type: ignore
    from teamwork import PATTERNS, validate_teamwork_plan  # type: ignore

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
MAX_REQUEST = 20000
PROVIDER = "opencode"
TEST_COMMAND = "python3.11 -m unittest discover -s tests -q"
STATUS_COMMAND = "git status --short"

RESEARCH_WORDS = (
    "research", "investigate", "investigation", "analyze", "analysis",
    "survey", "compare", "comparison", "literature", "study", "spike",
    "explore", "evaluate",
)
BUILD_WORDS = (
    "implement", "build", "create", "add", "develop", "write", "ship",
    "migrate", "upgrade", "integrate", "change",
)
REVIEW_WORDS = (
    "review", "audit", "refactor", "repair", "fix", "bug", "regression",
    "security", "harden", "cleanup",
)
BACKEND_WORDS = (
    "backend", "back-end", "back end", "server", "api", "database",
    "service", "microservice", "endpoint", "worker", "queue",
)
FRONTEND_WORDS = (
    "frontend", "front-end", "front end", "ui", "ux", "client", "browser",
    "react", "vue", "svelte", "css",
)
DISTRIBUTED_WORDS = (
    "distributed", "parallel", "multiple-services", "microservices",
    "cross-service", "full-stack", "full stack", "end-to-end",
)
BOTH_LAYER_WORDS = ("full-stack", "full stack", "end-to-end")


class TeamworkPlannerError(ValueError):
    """Invalid request/planner arguments or an unsafe template combination."""


PlannerError = TeamworkPlannerError


def _request_text(request):
    if isinstance(request, str):
        text = request
    elif isinstance(request, dict):
        candidates = [request.get(key) for key in
                      ("request", "text", "objective", "description")]
        text = " ".join(item for item in candidates if isinstance(item, str))
    else:
        raise TeamworkPlannerError("request must be a string or object")
    text = text.strip()
    if not text or len(text) > MAX_REQUEST:
        raise TeamworkPlannerError(f"request must be 1..{MAX_REQUEST} characters")
    return text


def _has(text, words):
    for word in words:
        pattern = r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            return True
    return False


def _select_pattern(text, explicit):
    if explicit is not None:
        return explicit
    research = _has(text, RESEARCH_WORDS)
    build = _has(text, BUILD_WORDS)
    review = _has(text, REVIEW_WORDS)
    backend = _has(text, BACKEND_WORDS)
    frontend = _has(text, FRONTEND_WORDS)
    both_layers = _has(text, BOTH_LAYER_WORDS)
    distributed = _has(text, DISTRIBUTED_WORDS)
    if research and not build:
        return "research"
    if (backend and frontend) or both_layers or distributed:
        return "distributed_coding"
    if review and not build:
        return "review_and_repair"
    return "iterative_coding"


def _shape(text, pattern):
    if pattern == "research":
        return "research"
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    if (backend or frontend) and not (backend and frontend):
        return "three"
    return "full"


def _task(task_id, role, milestone, objective, depends_on, read_scope,
          write_scope, acceptance, phase, verify_commands, department=None):
    repo_fields = []
    if write_scope:
        repo_fields.append("write_scope")
    repo_fields.append("verify_commands")
    return {
        "id": task_id,
        "role": role,
        "objective": objective,
        "milestone": milestone,
        "phase": phase,
        "depends_on": list(depends_on),
        "read_scope": list(read_scope),
        "write_scope": list(write_scope),
        "acceptance": list(acceptance),
        "verify_commands": list(verify_commands),
        "provider": PROVIDER,
        "model": None,  # Set by the template builder to the requested model.
        "department": department,
        "requires_repo": True,
        "repo_dependent_fields": repo_fields,
    }


def _milestone(milestone_id, objective, depends_on, acceptance, require_roles,
               max_repair=2):
    return {
        "id": milestone_id,
        "objective": objective,
        "depends_on": list(depends_on),
        "acceptance": list(acceptance),
        "verification_policy": {
            "require_roles": list(require_roles),
            "max_repair": max_repair,
        },
    }


def _discovery_task(model):
    task = _task(
        "discover", "explorer", "M1",
        "Map the repository areas, commands and risks needed for the requested change.",
        [], ["backend/", "frontend/", "src/", "tests/", "docs/"], [],
        [
            "Repository layout, entry points, and existing test commands are listed with file references.",
            "Risks and unknowns that need user confirmation are listed.",
            "No production file is modified during discovery.",
        ],
        "build", [STATUS_COMMAND], "research")
    task["model"] = model
    return task


def _research_task(model):
    task = _task(
        "research", "explorer", "M1",
        "Answer the research question and record sources, evidence and uncertainty.",
        [], ["src/", "tests/", "docs/"], [],
        [
            "Questions, sources, and evidence are recorded.",
            "Recommendations are separated from evidence and uncertainty.",
            "No repository file is modified during research.",
        ],
        "build", [STATUS_COMMAND], "research")
    task["model"] = model
    return task


def _build_task(model, task_id, department, path, objective, depends_on,
                milestone="M2"):
    task = _task(
        task_id, "implementer", milestone, objective, depends_on,
        [path, "tests/"], [path],
        [
            f"The requested change is implemented inside {path}.",
            "The recorded test command passes for the affected code.",
            f"No files outside {path} are modified.",
        ],
        "build", [TEST_COMMAND], department)
    task["model"] = model
    return task


def _generic_build_task(model, pattern):
    objective = ("Repair the requested behavior inside the declared source scope."
                 if pattern == "review_and_repair"
                 else "Implement the requested change inside the declared source scope.")
    task = _task(
        "build", "implementer", "M2", objective, ["discover"],
        ["src/", "tests/"], ["src/"],
        [
            "The requested change is implemented inside src/.",
            "The recorded test command passes for the affected code.",
            "No files outside src/ are modified.",
        ],
        "build", [TEST_COMMAND], None)
    task["model"] = model
    return task


def _verify_task(model, depends_on, read_scope, milestone="M3", task_id="verify"):
    task = _task(
        task_id, "tester", milestone,
        "Run the repository's real verification command and capture regression evidence.",
        depends_on, read_scope, ["tests/"],
        [
            "The repository test command exits 0 and its output is recorded.",
            "Regression coverage for the requested behavior is added or the gap is documented.",
            "Verification does not modify production files.",
        ],
        "verify", [TEST_COMMAND], "qa")
    task["model"] = model
    return task


def _review_task(model, task_id, role, objective, depends_on, acceptance,
                 milestone="M4", department="review"):
    task = _task(
        task_id, role, milestone, objective, depends_on,
        ["backend/", "frontend/", "src/", "tests/", "docs/"], [],
        acceptance, "verify", [TEST_COMMAND], department)
    task["model"] = model
    return task


def _audit_task(model, depends_on, milestone="M3"):
    task = _task(
        "audit", "auditor", milestone,
        "Audit scope, evidence and unresolved findings against every acceptance criterion.",
        depends_on, ["backend/", "frontend/", "src/", "tests/", "docs/"], [],
        [
            "Auditor checks scope, security, and evidence consistency.",
            "Unverified claims are listed as open issues rather than accepted.",
            "Audit does not modify production files.",
        ],
        "verify", [TEST_COMMAND], "review")
    task["model"] = model
    return task


def _full_plan(pattern, text, model):
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    tasks = [_discovery_task(model)]
    milestones = [
        _milestone("M1", "Discover the repository and the requested change.", [],
                   ["Discovery identifies the files, commands and risks needed for the change."],
                   []),
    ]
    build_ids = []
    if backend and frontend:
        backend_task = _build_task(
            model, "build-backend", "backend", "backend/",
            "Implement the backend part of the requested change.", ["discover"])
        frontend_task = _build_task(
            model, "build-frontend", "frontend", "frontend/",
            "Implement the frontend part of the requested change.", ["discover"])
        tasks.extend([backend_task, frontend_task])
        build_ids = ["build-backend", "build-frontend"]
    else:
        generic = _generic_build_task(model, pattern)
        # A missing layer keyword is not a reason to invent a second layer.
        if backend:
            generic["write_scope"] = ["backend/"]
            generic["read_scope"] = ["backend/", "tests/"]
            generic["department"] = "backend"
        elif frontend:
            generic["write_scope"] = ["frontend/"]
            generic["read_scope"] = ["frontend/", "tests/"]
            generic["department"] = "frontend"
        tasks.append(generic)
        build_ids = ["build"]
    milestones.append(
        _milestone("M2", "Implement the requested change.", ["M1"],
                   ["The requested change is implemented and its targeted tests pass."],
                   []))

    verify = _verify_task(
        model, list(build_ids),
        ["backend/", "frontend/", "src/", "tests/"])
    tasks.append(verify)
    milestones.append(
        _milestone("M3", "Verify the implementation.", ["M2"],
                   ["The full test command exits 0 and regression evidence is recorded."],
                   ["tester"]))

    review = _review_task(
        model, "review", "reviewer",
        "Review the implementation against the acceptance criteria and report findings.",
        ["verify"], [
            "Reviewer records blocking and non-blocking findings with file references.",
            "Every milestone acceptance criterion has evidence or an explicit gap.",
            "Review does not modify production files.",
        ])
    challenge = _review_task(
        model, "challenge", "challenger",
        "Challenge the implementation with adversarial and failure-path scenarios.",
        ["verify"], [
            "Challenger exercises at least one adversarial or failure-path scenario.",
            "Counterexamples are recorded with reproduction steps and expected behavior.",
            "Challenge does not modify production files.",
        ])
    audit = _review_task(
        model, "audit", "auditor",
        "Audit scope, security and evidence consistency independently.",
        ["verify"], [
            "Auditor checks scope, security, and evidence consistency.",
            "Unverified claims are listed as open issues rather than accepted.",
            "Audit does not modify production files.",
        ])
    tasks.extend([review, challenge, audit])
    milestones.append(
        _milestone("M4", "Review, challenge and audit the result.", ["M3"],
                   ["Review, challenge and audit verdicts are explicit."],
                   ["reviewer", "challenger", "auditor"]))

    success = _review_task(
        model, "success-audit", "success-auditor",
        "Reconcile all milestone evidence and issue the final success verdict.",
        ["review", "challenge", "audit"], [
            "Every milestone acceptance criterion is mapped to evidence.",
            "Open issues, skipped checks, and unverified claims are listed.",
            "The final verdict is explicit and does not infer success from pending work.",
        ], milestone="M5")
    tasks.append(success)
    milestones.append(
        _milestone("M5", "Issue the final success audit.", ["M4"],
                   ["All milestone evidence is reconciled and the final verdict is explicit."],
                   ["success-auditor"]))

    return milestones, tasks


def _three_plan(pattern, text, model):
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    if backend:
        task_id, department, path = "build-backend", "backend", "backend/"
    elif frontend:
        task_id, department, path = "build-frontend", "frontend", "frontend/"
    else:  # Defensive: _shape only returns "three" for one explicit layer.
        task_id, department, path = "build", None, "src/"
    objective = ("Repair the requested behavior inside the declared source scope."
                 if pattern == "review_and_repair"
                 else "Implement the requested change inside the declared source scope.")
    build = _build_task(model, task_id, department, path, objective, [],
                        milestone="M1")
    verify = _verify_task(
        model, [task_id], [path, "tests/"], milestone="M2", task_id="verify")
    audit = _audit_task(model, ["verify"], milestone="M3")
    milestones = [
        _milestone("M1", "Implement the requested change.", [],
                   ["The requested change is implemented.", "The declared tests pass."],
                   []),
        _milestone("M2", "Verify the implementation.", ["M1"],
                   ["The full test command exits 0 and output is recorded."],
                   ["tester"]),
        _milestone("M3", "Audit the implementation and evidence.", ["M2"],
                   ["Every acceptance criterion is checked against evidence.",
                    "Unresolved issues are explicit."],
                   ["auditor"]),
    ]
    return milestones, [build, verify, audit]


def _research_plan(model):
    milestones = [
        _milestone("M1", "Research the request and record evidence.", [],
                   ["Research questions are answered with cited evidence where available.",
                    "Recommendations and unknowns are separated."],
                   []),
    ]
    return milestones, [_research_task(model)]


def plan_request(request, *, run_id, project_id=None, max_parallel=3,
                 model=None, pattern=None):
    """Return a deterministic rich teamwork plan for ``request``."""
    try:
        run_id = identifier(run_id)
    except SupervisorError as exc:
        raise TeamworkPlannerError("Invalid run_id") from exc
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise TeamworkPlannerError("project_id must be p_<16 hex> when supplied")
    if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
        raise TeamworkPlannerError("max_parallel must be 1..3")
    if model is not None:
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise TeamworkPlannerError("model must be 1..200 characters")
        if any(ord(ch) < 32 for ch in model):
            raise TeamworkPlannerError("model must be a single-line value")
    if pattern is not None and pattern not in PATTERNS:
        raise TeamworkPlannerError("Unknown teamwork pattern")
    text = _request_text(request).lower()
    selected = _select_pattern(text, pattern)
    shape = _shape(text, selected)
    if shape == "research":
        milestones, tasks = _research_plan(model)
    elif shape == "three":
        milestones, tasks = _three_plan(selected, text, model)
    else:
        milestones, tasks = _full_plan(selected, text, model)
    used_layers = sorted({
        task["department"] for task in tasks
        if task["department"] in ("backend", "frontend")
    })
    repo_inputs = [
        "Confirm the real source paths; templates use "
        + (", ".join(path + "/" for path in used_layers) if used_layers
           else "src/")
        + " and tests/.",
        "Replace the template test command with the repository's real command.",
    ]
    if any(task["write_scope"] for task in tasks):
        repo_inputs.append("Confirm the package manager and build commands.")
    plan = {
        "version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "max_parallel": max_parallel,
        "pattern": selected,
        "requires_repo": True,
        "repo_inputs": repo_inputs,
        "repo_dependent_fields": {
            "tasks[].write_scope": "Template paths must be confirmed in the target repository.",
            "tasks[].verify_commands": "Template commands may not exist in the target repository.",
        },
        "notes": [
            "Deterministic template plan; no LLM call and no repository read was made.",
            "Supervisor still enforces leases, scopes and native evidence at execution time.",
            "Milestones pass only from persisted task results and required role verdicts.",
        ],
        "milestones": milestones,
        "tasks": tasks,
    }
    try:
        validate_teamwork_plan(plan)
    except ValueError as exc:  # Defensive: a template bug must not ship a bad plan.
        raise TeamworkPlannerError(f"Generated plan is invalid: {exc}") from exc
    return plan
