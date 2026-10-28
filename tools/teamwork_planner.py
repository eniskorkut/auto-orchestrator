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
    from tools.requirements_contract import (
        ContractError,
        compact_contract_ref,
        validate_for_plan,
    )
    from tools.supervisor import SupervisorError, identifier
    from tools.teamwork import (
        MAX_ACCEPTANCE,
        MAX_ACCEPTANCE_TEXT,
        MAX_VERIFY_COMMANDS,
        PATTERNS,
        validate_teamwork_plan,
    )
except ModuleNotFoundError:  # Running as ``python tools/teamwork_planner.py``.
    from requirements_contract import (  # type: ignore
        ContractError,
        compact_contract_ref,
        validate_for_plan,
    )
    from supervisor import SupervisorError, identifier  # type: ignore
    from teamwork import (  # type: ignore
        MAX_ACCEPTANCE,
        MAX_ACCEPTANCE_TEXT,
        MAX_VERIFY_COMMANDS,
        PATTERNS,
        validate_teamwork_plan,
    )

PROJECT_ID = re.compile(r"^p_[0-9a-f]{16}$")
MAX_REQUEST = 20000
PROVIDER = "opencode"
TEST_COMMAND = "python3.11 -m unittest discover -s tests -q"
STATUS_COMMAND = "git status --short"
MAX_OBJECTIVE = 1500
MAX_CONTRACT_OBJECTIVE_SUFFIX = 700
REVIEW_ROLES = frozenset({"reviewer", "challenger"})
AUDIT_ROLES = frozenset({"auditor", "success-auditor"})

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
WORK_MODES = ("solo", "fast", "balanced", "rigorous", "auto")
SECURITY_WORDS = (
    "security", "auth", "authentication", "authorization", "payment",
    "secret", "credential", "permission", "encryption", "tenant", "billing",
)
MIGRATION_WORDS = ("migration", "schema", "database", "sqlite", "postgres", "mongo")
DEPLOY_WORDS = ("deploy", "deployment", "production", "release", "rollout")
BREAKING_WORDS = ("breaking", "public api", "contract change", "backward incompatible")
FLOOR_TOPICS = frozenset({
    "auth_session", "payments", "data_retention", "database",
})
MIGRATION_TOPICS = frozenset({"database", "api_compat"})
HIGH_RISK_TOPICS = FLOOR_TOPICS | MIGRATION_TOPICS | frozenset({"file_access"})


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


def _profile_number(profile, key, default=0):
    if not isinstance(profile, dict):
        return default
    value = profile.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(0, value)


def _contract_topics(contract):
    if not isinstance(contract, dict):
        return set()
    topics = set()
    for item in contract.get("confirmed_decisions") or []:
        if isinstance(item, dict) and isinstance(item.get("topic"), str):
            topics.add(item["topic"].strip().lower())
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, dict):
            continue
        evidence = criterion.get("evidence") or {}
        topic = evidence.get("topic")
        if isinstance(topic, str) and topic:
            topics.add(topic.strip().lower())
    return topics


def _solo_high_risk(score, layers, security, migration, breaking, topics):
    return bool(security or migration or breaking or layers >= 2
                or score >= 34 or topics & HIGH_RISK_TOPICS)


def assess_complexity(request, *, repo_profile=None, requested_mode=None,
                      requirements_contract=None):
    """Return a transparent complexity assessment, never a fake certainty.

    The score is an aid for choosing a coordination budget. Keyword signals and
    repository facts are listed separately; a missing repository profile lowers
    confidence and falls back to BALANCED rather than pretending SOLO/FAST is
    safe. AUTO also applies non-negotiable risk floors from approved contract
    decisions.
    """
    text = _request_text(request).lower()
    if requested_mode is not None and requested_mode not in WORK_MODES:
        raise TeamworkPlannerError(
            "mode must be solo, fast, balanced, rigorous or auto")
    contract = (requirements_contract
                if isinstance(requirements_contract, dict) else None)
    contract_topics = _contract_topics(contract)
    text_topics = set()
    if _has(text, ("payment", "payments", "checkout", "billing", "invoice")):
        text_topics.add("payments")
    if _has(text, ("retention", "data retention", "soft delete",
                   "hard delete", "delete policy", "delete account",
                   "account deletion", "remove user", "remove users",
                   "purge", "erase", "wipe", "gdpr", "kvkk", "veri saklama",
                   "kalıcı sil", "kalici sil")):
        text_topics.add("data_retention")
    if _has(text, MIGRATION_WORDS):
        text_topics.add("database")
    if _has(text, SECURITY_WORDS):
        text_topics.add("auth_session")
    if _has(text, ("file access", "file sharing", "shared file", "upload",
                   "download", "file delete")):
        text_topics.add("file_access")
    if _has(text, BREAKING_WORDS):
        text_topics.add("api_compat")
    risk_topics = contract_topics | text_topics
    approved_contract = bool(contract and contract.get("status") == "confirmed")
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    security = _has(text, SECURITY_WORDS) or bool(
        risk_topics & {"auth_session", "payments", "data_retention",
                       "file_access"})
    migration = (_has(text, MIGRATION_WORDS)
                 or bool(risk_topics & MIGRATION_TOPICS))
    deploy = _has(text, DEPLOY_WORDS)
    breaking = _has(text, BREAKING_WORDS) or "api_compat" in risk_topics
    review = _has(text, REVIEW_WORDS)
    profile = repo_profile if isinstance(repo_profile, dict) else {}
    files = _profile_number(profile, "files")
    modules = _profile_number(profile, "modules")
    tests = _profile_number(profile, "tests")
    languages = profile.get("languages") if isinstance(profile.get("languages"), list) else []
    profile_backend = bool(profile.get("backend"))
    profile_frontend = bool(profile.get("frontend"))
    if profile_backend:
        backend = True
    if profile_frontend:
        frontend = True
    layers = int(backend) + int(frontend)
    factors = {
        "layers": layers,
        "security": security,
        "migration": migration,
        "deploy": deploy,
        "breaking": breaking,
        "review": review,
        "profile_files": files,
        "profile_modules": modules,
        "profile_tests": tests,
        "profile_languages": len(languages),
        "profile_available": bool(profile),
        "contract_topics": sorted(contract_topics),
        "risk_topics": sorted(risk_topics),
        "contract_approved": approved_contract,
    }
    score = 0
    score += 10 if layers >= 2 else 3
    score += 8 if security else 0
    score += 8 if migration else 0
    score += 5 if deploy else 0
    score += 10 if breaking else 0
    score += 5 if review else 0
    score += min(20, files // 25)
    score += min(12, modules)
    score += 4 if tests >= 20 else 0
    score += min(12, len(languages) * 2)
    score = min(100, score)
    confidence = "high" if profile and files > 0 else "low"
    safety_upgrade = False
    auto_floor = None
    if requested_mode == "solo":
        if _solo_high_risk(score, layers, security, migration, breaking,
                           risk_topics):
            mode = "balanced" if score < 50 else "rigorous"
            safety_upgrade = True
            reason = ("safety upgrade: SOLO is not allowed for high-risk, "
                      "multi-layer, security, migration or breaking work")
        else:
            mode = "solo"
            reason = "explicit SOLO for a small, low-risk change"
    elif requested_mode in ("fast", "balanced", "rigorous"):
        mode = requested_mode
        reason = "explicit user mode"
        if requested_mode == "fast" and (security or migration or breaking):
            mode = "balanced" if score < 50 else "rigorous"
            safety_upgrade = True
            reason = ("safety upgrade: FAST is not allowed for security, "
                      "migration or breaking-change work")
    elif score >= 50:
        mode = "rigorous"
        reason = "high risk/complexity score"
    elif (migration or breaking or bool(risk_topics & MIGRATION_TOPICS)) \
            and approved_contract:
        mode = "rigorous"
        auto_floor = "migration/api_compat + approved contract"
        reason = "AUTO floor: migration or API compatibility work needs rigorous verification"
    elif security or bool(risk_topics & FLOOR_TOPICS):
        mode = "balanced"
        auto_floor = "auth/payments/data retention/database"
        reason = "AUTO floor: authentication, payments, retention or database work is at least balanced"
    elif (score <= 18 and layers <= 1 and not security and not migration
          and not breaking):
        mode = "solo" if confidence == "high" else "balanced"
        reason = ("small, clear, low-risk repository change" if mode == "solo"
                  else "small request but repository profile is missing")
        if mode == "solo" and confidence == "high":
            reason = "small, clear, low-risk repository change"
    else:
        mode = "balanced"
        reason = "default safe budget"
    return {
        "mode": mode,
        "requested_mode": requested_mode,
        "score": score,
        "confidence": confidence,
        "reason": reason,
        "factors": factors,
        "safety_upgrade": safety_upgrade,
        "auto_floor": auto_floor,
        "fallback_policy": "BALANCED when repository facts are missing",
    }


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


def _contract_objective_suffix(contract):
    """Bounded constraints/decisions appended to build-phase objectives."""
    parts = []
    constraints = contract.get("constraints") or []
    if constraints:
        parts.append("Contract constraints: " + "; ".join(constraints))
    decisions = contract.get("confirmed_decisions") or []
    if decisions:
        parts.append("Confirmed decisions: " + "; ".join(
            f"{item['topic']}={item['decision']}" for item in decisions))
    if not parts:
        return ""
    return "\n".join(parts)[:MAX_CONTRACT_OBJECTIVE_SUFFIX]


def _append_contract_objective(task, suffix):
    if not suffix:
        return
    objective = task.get("objective")
    if not isinstance(objective, str) or suffix in objective:
        return
    available = MAX_OBJECTIVE - len(objective) - 1
    if available <= 0:
        return
    task["objective"] = objective + "\n" + suffix[:available]


def _task_contract_phase(task):
    """Map a teamwork task to the contract phase whose criteria it owns."""
    phase = task.get("phase")
    role = task.get("role")
    if phase in ("build", "repair"):
        return "build"
    if role in REVIEW_ROLES:
        return "review"
    if role in AUDIT_ROLES:
        return "audit"
    return "verify"


def _acceptance_labels(contract):
    labels = []
    for criterion in contract.get("acceptance_criteria") or []:
        label = f"AC {criterion['id']}: {criterion['text']}"
        if len(label) > MAX_ACCEPTANCE_TEXT:
            label = label[:MAX_ACCEPTANCE_TEXT]
        labels.append((criterion.get("applies_to") or [], label))
    return labels


def _apply_requirements_contract(tasks, contract):
    """Attach contract constraints, criteria and validated commands.

    Read/write scopes are never touched: contract evidence must not widen what
    an agent is allowed to change.
    """
    suffix = _contract_objective_suffix(contract)
    labels = _acceptance_labels(contract)
    for task in tasks:
        phase = _task_contract_phase(task)
        if phase == "build":
            _append_contract_objective(task, suffix)
            continue
        acceptance = task.setdefault("acceptance", [])
        for applies_to, label in labels:
            if phase not in applies_to or label in acceptance:
                continue
            if len(acceptance) >= MAX_ACCEPTANCE:
                break
            acceptance.append(label)
    commands = [
        criterion["evidence"]["command"]
        for criterion in contract.get("acceptance_criteria") or []
        if criterion.get("evidence", {}).get("kind") == "command"
    ]
    contract_command = contract.get("verification_command")
    if isinstance(contract_command, str) and contract_command:
        commands = [contract_command, *commands]
    targets = [task for task in tasks
               if task.get("phase") == "verify" and task.get("role") == "tester"]
    if not targets:
        targets = [task for task in tasks if task.get("phase") == "verify"]
    if not targets:
        # A SOLO plan has no separate tester/reviewer. The implementer owns
        # both the change and the deterministic verification command.
        targets = [task for task in tasks if task.get("role") == "implementer"][:1]
        for target in targets:
            acceptance = target.setdefault("acceptance", [])
            for _applies_to, label in labels:
                if label in acceptance:
                    continue
                if len(acceptance) >= MAX_ACCEPTANCE:
                    break
                acceptance.append(label)
    if not targets:
        return
    target = targets[0]
    verify_commands = target.setdefault("verify_commands", [])
    for command in dict.fromkeys(commands):
        if command in verify_commands:
            continue
        if len(verify_commands) >= MAX_VERIFY_COMMANDS:
            break
        verify_commands.append(command)


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
        "build", [STATUS_COMMAND], department)
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
        "build", [STATUS_COMMAND], None)
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


def _fast_plan(pattern, text, model):
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    tasks = []
    build_ids = []
    if backend and frontend:
        tasks.extend([
            _build_task(model, "build-backend", "backend", "backend/",
                        "Implement the small backend change.", [], milestone="M1"),
            _build_task(model, "build-frontend", "frontend", "frontend/",
                        "Implement the small frontend change.", [], milestone="M1"),
        ])
        build_ids = ["build-backend", "build-frontend"]
    else:
        task_id = "build-backend" if backend else ("build-frontend" if frontend else "build")
        department = "backend" if backend else ("frontend" if frontend else None)
        path = "backend/" if backend else ("frontend/" if frontend else "src/")
        tasks.append(_build_task(
            model, task_id, department, path,
            "Implement the small requested change.", [], milestone="M1"))
        build_ids = [task_id]
    tasks.append(_verify_task(
        model, build_ids, ["backend/", "frontend/", "src/", "tests/"],
        milestone="M1", task_id="verify"))
    milestone = _milestone(
        "M1", "Implement and verify the small change.", [],
        ["The requested change is implemented.", "Targeted tests pass."],
        [], max_repair=2)
    return [milestone], tasks


def _solo_plan(pattern, text, model):
    """One implementer, one milestone, deterministic self-verification."""
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    if backend:
        task_id, department, path = "build-backend", "backend", "backend/"
    elif frontend:
        task_id, department, path = "build-frontend", "frontend", "frontend/"
    else:
        task_id, department, path = "build", None, "src/"
    objective = ("Repair the requested behavior inside the declared source scope."
                 if pattern == "review_and_repair"
                 else "Implement the requested change inside the declared source scope.")
    build = _task(
        task_id, "implementer", "M1", objective, [],
        [path, "tests/"], [path],
        [
            f"The requested change is implemented inside {path}.",
            "The deterministic verification commands exit 0 and their output is recorded.",
            f"No files outside {path} are modified.",
        ],
        "build", [STATUS_COMMAND, TEST_COMMAND], department)
    build["model"] = model
    milestone = _milestone(
        "M1", "Implement and self-verify the small change.", [],
        ["The requested change is implemented.",
         "The deterministic verification commands pass."],
        [], max_repair=2)
    return [milestone], [build]


def _balanced_plan(pattern, text, model):
    backend = _has(text, BACKEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    frontend = _has(text, FRONTEND_WORDS) or _has(text, BOTH_LAYER_WORDS)
    tasks = []
    build_ids = []
    if backend and frontend:
        tasks.extend([
            _build_task(model, "build-backend", "backend", "backend/",
                        "Implement the backend part of the requested change.", [],
                        milestone="M1"),
            _build_task(model, "build-frontend", "frontend", "frontend/",
                        "Implement the frontend part of the requested change.", [],
                        milestone="M1"),
        ])
        build_ids = ["build-backend", "build-frontend"]
    else:
        task_id = "build-backend" if backend else ("build-frontend" if frontend else "build")
        department = "backend" if backend else ("frontend" if frontend else None)
        path = "backend/" if backend else ("frontend/" if frontend else "src/")
        tasks.append(_build_task(
            model, task_id, department, path,
            "Implement the requested change.", [], milestone="M1"))
        build_ids = [task_id]
    tasks.append(_verify_task(
        model, build_ids, ["backend/", "frontend/", "src/", "tests/"],
        milestone="M2", task_id="verify"))
    review = _review_task(
        model, "review", "reviewer",
        "Risk-based independent review of the integrated change.",
        ["verify"], [
            "Reviewer records blocking and non-blocking findings.",
            "High-risk paths are checked explicitly; lower-risk areas are sampled.",
            "Review does not modify production files.",
        ], milestone="M3")
    audit = _audit_task(model, ["verify"], milestone="M3")
    tasks.extend([review, audit])
    milestones = [
        _milestone("M1", "Implement the requested change.", [],
                   ["The requested change is implemented."], []),
        _milestone("M2", "Run targeted and regression verification.", ["M1"],
                   ["The test command exits 0 and output is recorded."],
                   ["tester"]),
        _milestone("M3", "Review risks and audit evidence.", ["M2"],
                   ["Reviewer and auditor verdicts are explicit."],
                   ["reviewer", "auditor"]),
    ]
    return milestones, tasks


INTEGRITY_POLICIES = ("development", "demo", "benchmark")


def _scope_contract(request, plan, contract):
    tasks = plan.get("tasks") or []
    scope = sorted({path for task in tasks for path in task.get("write_scope") or []})
    deliverables = [str(task.get("id")) for task in tasks
                    if isinstance(task.get("id"), str)][:40]
    requirements = []
    if isinstance(contract, dict):
        requirements = [
            str(item.get("text"))[:500] for item in
            (contract.get("acceptance_criteria") or [])
            if isinstance(item, dict) and item.get("text")][:60]
    return {
        "objective": _request_text(request)[:500],
        "in_scope": scope[:60],
        "non_goals": [
            "Changes outside the confirmed write scopes",
            "Secrets, credentials or production data",
            "Unapproved dependency or schema changes",
        ],
        "requirements": requirements,
        "deliverables": deliverables,
        "rollback": [
            "Revert the run's commits on the integration branch.",
            "Discard the run's worktrees and scratch directories.",
            "Project memory and approved contracts are append-only audit records.",
        ],
        "constraints": list((contract or {}).get("constraints") or [])[:40]
        if isinstance(contract, dict) else [],
    }


def _file_ownership(plan):
    manifest = []
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        manifest.append({
            "task_id": task.get("id"),
            "role": task.get("role"),
            "milestone": task.get("milestone"),
            "write_scope": list(task.get("write_scope") or []),
            "depends_on": list(task.get("depends_on") or []),
            "exclusive": True,
        })
    return manifest


def _scope_key(path):
    return str(path).replace("\\", "/").strip("/")


def _scopes_overlap(left, right):
    for a in left:
        for b in right:
            a, b = _scope_key(a), _scope_key(b)
            if not a or not b:
                continue
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def _ownership_conflicts(plan):
    tasks = [task for task in plan.get("tasks") or []
             if isinstance(task, dict)]
    by_id = {task.get("id"): task for task in tasks}

    def depends_on(start, target, seen=None):
        seen = seen or set()
        if start in seen:
            return False
        seen.add(start)
        task = by_id.get(start)
        if not isinstance(task, dict):
            return False
        for dependency in task.get("depends_on") or []:
            if dependency == target or depends_on(dependency, target, seen):
                return True
        return False

    conflicts = []
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            if ((left.get("milestone") or "") != (right.get("milestone") or "")):
                continue
            if (depends_on(left.get("id"), right.get("id"))
                    or depends_on(right.get("id"), left.get("id"))):
                continue
            if _scopes_overlap(left.get("write_scope") or [],
                               right.get("write_scope") or []):
                conflicts.append({
                    "left": left.get("id"),
                    "right": right.get("id"),
                    "milestone": left.get("milestone"),
                    "scopes": sorted(set(left.get("write_scope") or [])
                                     & set(right.get("write_scope") or [])),
                })
    return conflicts


def plan_request(request, *, run_id, project_id=None, max_parallel=3,
                 model=None, pattern=None, mode=None, repo_profile=None,
                 provider="opencode", requirements_contract=None,
                 codex_sandbox="read-only", integrity_policy="development"):
    """Return a deterministic rich teamwork plan for ``request``."""
    try:
        run_id = identifier(run_id)
    except SupervisorError as exc:
        raise TeamworkPlannerError("Invalid run_id") from exc
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise TeamworkPlannerError("project_id must be p_<16 hex> when supplied")
    if codex_sandbox not in ("read-only", "workspace-write"):
        raise TeamworkPlannerError("codex_sandbox must be read-only or workspace-write")
    contract = None
    raw_contract = requirements_contract
    if requirements_contract is not None:
        if project_id is None:
            raise TeamworkPlannerError(
                "requirements_contract requires an explicit project_id")
        try:
            contract = validate_for_plan(requirements_contract,
                                         project_id=project_id)
        except ContractError as exc:
            raise TeamworkPlannerError(
                f"Invalid requirements contract: {exc}") from exc
    if type(max_parallel) is not int or not 1 <= max_parallel <= 3:
        raise TeamworkPlannerError("max_parallel must be 1..3")
    if model is not None:
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise TeamworkPlannerError("model must be 1..200 characters")
        if any(ord(ch) < 32 for ch in model):
            raise TeamworkPlannerError("model must be a single-line value")
    if provider not in ("opencode", "codex"):
        raise TeamworkPlannerError("provider must be opencode or codex")
    if integrity_policy not in INTEGRITY_POLICIES:
        raise TeamworkPlannerError(
            "integrity_policy must be development, demo or benchmark")
    if pattern is not None and pattern not in PATTERNS:
        raise TeamworkPlannerError("Unknown teamwork pattern")
    if mode is not None and mode not in WORK_MODES:
        raise TeamworkPlannerError(
            "mode must be solo, fast, balanced, rigorous or auto")
    text = _request_text(request).lower()
    selected = _select_pattern(text, pattern)
    shape = _shape(text, selected)
    assessment = None
    chosen_mode = mode
    if mode in WORK_MODES:
        assessment = assess_complexity(
            request, repo_profile=repo_profile, requested_mode=mode,
            requirements_contract=contract)
        chosen_mode = assessment["mode"]
    if shape == "research":
        milestones, tasks = _research_plan(model)
    elif chosen_mode == "solo":
        milestones, tasks = _solo_plan(selected, text, model)
    elif chosen_mode == "fast":
        milestones, tasks = _fast_plan(selected, text, model)
    elif chosen_mode == "balanced":
        milestones, tasks = _balanced_plan(selected, text, model)
    elif chosen_mode == "rigorous":
        milestones, tasks = _full_plan(selected, text, model)
    elif mode is None:
        # Legacy template behavior remains stable for callers that do not use
        # the adaptive modes.
        if shape == "three":
            milestones, tasks = _three_plan(selected, text, model)
        else:
            milestones, tasks = _full_plan(selected, text, model)
    else:  # explicit auto must have resolved to a concrete mode
        milestones, tasks = _balanced_plan(selected, text, model)
    detected_commands = []
    command_source = None
    if isinstance(repo_profile, dict):
        sources = repo_profile.get("test_command_sources")
        if isinstance(sources, list):
            for source in sources[:4]:
                if not isinstance(source, dict):
                    continue
                candidate = source.get("command")
                if (isinstance(candidate, str) and candidate.strip()
                        and len(candidate) <= 500
                        and candidate.strip() not in detected_commands):
                    detected_commands.append(candidate.strip())
                    if command_source is None:
                        command_source = source.get("source")
    detected_command = detected_commands[0] if detected_commands else None
    if (contract is not None and detected_command
            and isinstance(raw_contract, dict)
            and raw_contract.get("sha256") is None):
        # Draft contracts (no immutable digest yet) may adopt the repository's
        # real test command. Approved/digest-bound contracts are never mutated;
        # the plan-level verification_command carries the execution override.
        candidate = {key: value for key, value in contract.items()
                     if key != "sha256"}
        candidate["verification_command"] = detected_command
        try:
            contract = validate_for_plan(candidate, project_id=project_id)
        except ContractError:
            pass
    if detected_commands:
        for task in tasks:
            replaced = []
            for command in task.get("verify_commands", []):
                if command == TEST_COMMAND:
                    replaced.extend(detected_commands)
                elif command not in replaced:
                    replaced.append(command)
            task["verify_commands"] = replaced
    used_layers = sorted({
        task["department"] for task in tasks
        if task["department"] in ("backend", "frontend")
    })
    repo_inputs = [
        "Confirm the real source paths; templates use "
        + (", ".join(path + "/" for path in used_layers) if used_layers
           else "src/")
        + " and tests/.",
        ("Confirm the detected test command: " + detected_command
         if detected_command else
         "Replace the template test command with the repository's real command."),
    ]
    if any(task["write_scope"] for task in tasks):
        repo_inputs.append("Confirm the package manager and build commands.")
    effective_parallel = 1 if chosen_mode == "solo" else max_parallel
    plan = {
        "version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "max_parallel": effective_parallel,
        "pattern": selected,
        "work_mode": chosen_mode or "legacy",
        "risk_assessment": assessment,
        "test_command": detected_command or TEST_COMMAND,
        "test_commands": detected_commands or [TEST_COMMAND],
        "test_command_source": command_source or "planner default",
        "verification_budget": {
            "mode": chosen_mode or "legacy",
            "risk_score": assessment.get("score") if assessment else None,
            "max_parallel": effective_parallel,
            "repair_policy": (
                "bounded by milestone max_repair"
                if chosen_mode in ("solo", "fast", "balanced", "rigorous")
                else "legacy milestone policy"
            ),
            "review_depth": {
                "solo": "self-verified",
                "fast": "targeted",
                "balanced": "risk-based",
                "rigorous": "full",
            }.get(chosen_mode, "legacy"),
            "token_budget": None,
            "note": "Token/cost budget is not fabricated; provider usage is measured at run time.",
        },
        "request": _request_text(request),
        "repo_profile": {
            key: repo_profile.get(key) for key in
            ("files", "modules", "tests", "languages", "backend", "frontend")
        } if isinstance(repo_profile, dict) else None,
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
        "plan_version": 2,
        "scope_contract": _scope_contract(request, {"tasks": tasks}, contract),
        "file_ownership": _file_ownership({"tasks": tasks}),
        "ownership_conflicts": _ownership_conflicts({"tasks": tasks}),
        "integrity_policy": integrity_policy,
        "adaptive_decision": (
            {
                "requested_mode": assessment.get("requested_mode"),
                "chosen_mode": assessment.get("mode"),
                "score": assessment.get("score"),
                "confidence": assessment.get("confidence"),
                "reason": assessment.get("reason"),
                "factors": assessment.get("factors"),
                "safety_upgrade": assessment.get("safety_upgrade"),
                "auto_floor": assessment.get("auto_floor"),
            } if isinstance(assessment, dict) else None),
    }
    if plan["ownership_conflicts"] and effective_parallel > 1:
        ids = ", ".join(
            f"{item['left']}~{item['right']}"
            for item in plan["ownership_conflicts"][:5])
        raise TeamworkPlannerError(
            "concurrent tasks share a write scope without a dependency: " + ids)
    if contract is not None:
        _apply_requirements_contract(tasks, contract)
        plan["requirements_contract"] = contract
        plan["contract_ref"] = compact_contract_ref(contract)
        # The plan may use a repository test command in place of the contract
        # default for execution. The immutable contract digest is not rewritten.
        plan["verification_command"] = (
            detected_command or contract.get("verification_command") or TEST_COMMAND)
    for task in tasks:
        task["provider"] = provider
        if provider == "codex":
            # Write mode must be explicitly requested. Approval bridging is not
            # implemented; Codex tasks always use approvalPolicy never.
            task.setdefault("sandbox", codex_sandbox)
            task.setdefault("approval_policy", "never")
    try:
        validate_teamwork_plan(plan)
    except ValueError as exc:  # Defensive: a template bug must not ship a bad plan.
        raise TeamworkPlannerError(f"Generated plan is invalid: {exc}") from exc
    return plan
