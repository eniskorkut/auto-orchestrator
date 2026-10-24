#!/usr/bin/env python3
"""Build compact, scoped subagent briefs and discover relevant installed skills."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from collections import Counter
try:
    from tools.supervisor import validate_plan, SupervisorError
except ModuleNotFoundError:
    from supervisor import validate_plan, SupervisorError

# Skill selection must be conservative: a single incidental description word
# must not route backend work to an animation/UI skill.
WORDS = re.compile(r"[^\W_]+", re.UNICODE)
STOP = {"agent", "agents", "skill", "skills", "task", "tasks", "code", "coding",
        "file", "files", "implement", "implementation", "review", "reviews",
        "this", "that", "those", "these", "with", "from", "must", "and", "the",
        "for", "using", "should", "work", "works", "create", "build", "make",
        "change", "update", "new", "add", "fix", "check", "best", "support",
        "help", "manage", "process", "project", "projects", "tool", "tools",
        "system", "systems", "feature", "features", "general", "complex",
        "simple", "use", "into", "also", "only", "more", "most", "some", "any",
        "all", "our", "your", "their", "its", "it", "is", "are", "be", "to",
        "of", "in", "on", "at", "by", "or", "as", "an", "a", "we", "you", "i",
        "ve", "ile", "için", "bir", "ve", "bu", "şu", "olan", "olarak"}

# Multi-word phrases carry more signal than isolated tokens. They are general
# domain vocabulary, not task-specific strings.
PHRASES = {
    "health check": {"backend": 3}, "race condition": {"backend": 3},
    "background worker": {"backend": 3}, "message queue": {"backend": 2},
    "request validation": {"backend": 2}, "rate limit": {"backend": 2},
    "component library": {"frontend": 2}, "dark mode": {"frontend": 3},
    "navigation bar": {"frontend": 3}, "bottom sheet": {"animation": 2, "frontend": 2},
    "drag and drop": {"frontend": 2}, "static regeneration": {"frontend": 3},
    "screen reader": {"accessibility": 3}, "api key": {"security": 3},
    "password reset": {"security": 3}, "session fixation": {"security": 3},
    "sql injection": {"security": 3}, "foreign key": {"database": 3},
    "explain plan": {"database": 3}, "unit test": {"testing": 3},
    "integration test": {"testing": 3}, "end to end": {"testing": 3},
    "github actions": {"devops": 3}, "gitlab ci": {"devops": 3},
    "vector database": {"ai": 3}, "knowledge base": {"ai": 3},
    "tool calling": {"ai": 3}, "prompt variants": {"ai": 3},
    "api reference": {"docs": 3}, "technical debt": {"refactor": 3},
    "sort imports": {"refactor": 2}, "audit log": {"backend": 3},
    "contract test": {"testing": 3}, "lock timeout": {"database": 3},
    "date picker": {"frontend": 2, "accessibility": 1},
    "open redirect": {"security": 3}, "s3 bucket": {"devops": 3},
    "property based": {"testing": 3}, "multi arch": {"devops": 3},
    "container image": {"devops": 3}, "layout shift": {"frontend": 2, "performance": 2},
    "design system": {"frontend": 2}, "reset token": {"security": 3},
    "event sourcing": {"backend": 2, "research": 2},
    "mutation testing": {"testing": 3}, "container image": {"devops": 3},
    "güvenlik açığı": {"security": 3}, "veritabanı şeması": {"database": 3},
    "docker imajı": {"devops": 2}, "yetkilendirme testleri": {"security": 2, "testing": 2},
}

DOMAIN_TERMS = {
    "backend": {"backend", "api", "endpoint", "route", "handler", "server",
                "service", "rest", "graphql", "grpc", "request", "response",
                "middleware", "controller", "webhook", "json", "http", "fastapi",
                "flask", "django", "express", "nestjs", "spring", "worker",
                "queue", "cron", "resolver", "validation", "serialization",
                "pagination", "blueprint", "payment", "error", "session", "import",
                "cursor", "audit", "status", "email", "signup", "response",
                "yanıt", "booking", "iso", "structured", "return"},
    "frontend": {"frontend", "ui", "ux", "react", "vue", "angular", "svelte",
                 "css", "html", "dom", "component", "layout", "page", "navbar",
                 "modal", "responsive", "tailwind", "theme", "style", "styling",
                 "form", "button", "dashboard", "card", "kanban", "overflow",
                 "navigation", "interface", "dropdown", "menu", "z", "stacking",
                 "tooltip", "popover", "carousel", "grid", "flex", "spacing",
                 "margin", "padding", "font", "color", "picker", "date", "select",
                 "input", "toggle", "badge", "scroll", "viewport", "px",
                 "pricing", "tab", "tabs", "combobox", "aria", "renderer",
                 "sticky", "header", "overlap", "skeleton", "shimmer", "bundle",
                 "site"},
    "animation": {"animation", "motion", "transition", "micro", "interaction",
                  "spring", "easing", "keyframe", "stagger", "fade", "slide",
                  "toast", "accordion", "hover", "jank", "gsap", "framer",
                  "crossfade", "notification", "bildirim", "shimmer",
                  "skeleton"},
    "security": {"security", "secure", "auth", "authentication", "authorization",
                 "oauth", "jwt", "csrf", "xss", "injection", "vulnerability",
                 "owasp", "secret", "credential", "password", "replay",
                 "hardening", "harden", "encryption", "permission", "rbac",
                 "sanitize", "audit", "leak", "leaked", "rotate", "jwt",
                 "redirect", "login", "rbac", "escape", "verification",
                 "verify", "signature", "impersonation", "csp", "header"},
    "testing": {"test", "pytest", "unittest", "jest", "vitest", "playwright",
                "cypress", "e2e", "coverage", "fixture", "mock", "stub",
                "regression", "flaky", "assertion", "tdd", "contract",
                "property", "suite", "mutation"},
    "devops": {"docker", "dockerfile", "container", "containerize", "kubernetes",
               "k8s", "helm", "terraform", "ansible", "ci", "cd", "pipeline",
               "deployment", "deploy", "release", "observability", "logs",
               "metrics", "monitoring", "prometheus", "grafana", "vpc", "cloud",
               "aws", "gcp", "azure", "nginx", "s3", "provision", "trace",
               "tracing", "traces", "opentelemetry", "telemetry", "span",
               "multiarch", "cron", "resource", "limits", "staging", "canary",
               "autoscaling", "pool", "downtime", "logging"},
    "ai": {"rag", "llm", "embedding", "vector", "retrieval", "rerank", "chunk",
           "chunking", "prompt", "hallucination", "agent", "openai", "anthropic",
           "langchain", "llama", "finetune", "inference", "citation", "semantic",
           "pgvector", "embeddings", "store"},
    "database": {"database", "sql", "postgres", "postgresql", "mysql", "sqlite",
                 "mongodb", "schema", "migration", "index", "query", "transaction",
                 "deadlock", "join", "orm", "normalization", "normalized", "table",
                 "column", "redis", "elasticsearch", "lock", "backfill",
                 "tenant", "partition", "constraint", "partial", "subquery",
                 "correlated", "rewrite"},
    "data": {"pandas", "dataframe", "csv", "dataset", "analytics", "statistics",
             "visualization", "chart", "etl", "warehouse", "numpy", "jupyter"},
    "docs": {"documentation", "docs", "readme", "tutorial", "changelog", "guide",
             "manual"},
    "research": {"research", "investigate", "compare", "survey", "benchmark",
                 "evaluate", "explore", "feasibility", "tradeoff", "spike",
                 "options", "study", "tradeoffs", "monorepo"},
    "refactor": {"refactor", "duplication", "maintainability", "restructure",
                 "cleanup"},
    "mobile": {"ios", "android", "swift", "swiftui", "kotlin", "mobile"},
    "performance": {"performance", "latency", "throughput", "memory", "cpu",
                    "profile", "profiling", "optimize", "slow", "bottleneck",
                    "flamegraph", "cache"},
    "accessibility": {"accessibility", "wcag", "a11y", "keyboard", "focus",
                      "contrast", "aria"},
}

# Specific terms are stronger than broad ones (e.g. "json" alone should not
# imply backend work, while "schema" or "dockerfile" is a real signal).
TERM_WEIGHTS = {
    "fastapi": 2, "graphql": 2, "grpc": 2, "endpoint": 2, "webhook": 2,
    "resolver": 2, "blueprint": 2, "payment": 2, "route": 2, "component": 2,
    "accordion": 2, "style": 2, "focus": 2, "import": 2,
    "react": 2, "css": 2, "responsive": 2, "navbar": 2, "modal": 2,
    "animation": 2, "motion": 2, "transition": 2, "toast": 2, "jank": 2,
    "overflow": 2, "redirect": 2, "login": 2, "rbac": 2, "accessible": 2,
    "dropdown": 2, "z": 2, "stacking": 2, "picker": 2, "provision": 2,
    "trace": 2, "tracing": 2, "traces": 2, "telemetry": 2, "contract": 2,
    "lock": 2, "backfill": 2, "partition": 2, "escape": 2, "verification": 2,
    "verify": 2, "signature": 2, "jwt": 2, "csp": 2, "scroll": 2, "tabs": 2,
    "combobox": 2, "aria": 2, "crossfade": 2, "notification": 2, "pgvector": 2,
    "reranking": 2, "multiarch": 2, "cron": 2, "image": 2, "study": 2,
    "tradeoffs": 2, "property": 2, "suite": 2, "status": 2, "email": 2,
    "signup": 2, "response": 2, "staging": 2, "partial": 2, "sticky": 2,
    "header": 2, "overlap": 2, "skeleton": 2, "shimmer": 2, "bundle": 2,
    "ssrf": 3, "signing": 2, "privilege": 2, "mutation": 2, "canary": 3,
    "autoscaling": 3, "subquery": 3, "correlated": 2, "rewrite": 2,
    "monorepo": 2, "booking": 2, "downtime": 2, "logging": 2,
    "authentication": 2, "authorization": 2, "vulnerability": 2, "xss": 2,
    "csrf": 2, "replay": 2, "leak": 2, "leaked": 2, "harden": 2,
    "pytest": 2, "playwright": 2, "coverage": 2, "e2e": 2, "fixture": 2,
    "flaky": 2, "regression": 2,
    "docker": 2, "dockerfile": 2, "containerize": 2, "kubernetes": 2,
    "terraform": 3, "helm": 3, "ci": 2, "cd": 2, "pipeline": 2,
    "deployment": 2, "deploy": 2, "metrics": 2, "logs": 2, "vpc": 3,
    "rag": 2, "llm": 2, "embedding": 2, "vector": 2, "retrieval": 2,
    "rerank": 2, "chunk": 2, "chunking": 2, "prompt": 2, "hallucination": 2,
    "schema": 2, "migration": 2, "index": 2, "query": 2, "transaction": 2,
    "deadlock": 2, "postgres": 2, "postgresql": 2, "sql": 2, "normalized": 2,
    "pandas": 2, "dataframe": 2, "csv": 2, "dataset": 2, "analytics": 2,
    "statistics": 2, "visualization": 2, "etl": 2,
    "documentation": 2, "readme": 2, "tutorial": 2, "changelog": 2,
    "benchmark": 4, "compare": 3, "survey": 3, "investigate": 2, "explore": 2,
    "research": 2, "tradeoff": 2,
    "refactor": 2, "duplication": 2, "maintainability": 2,
    "ios": 2, "android": 2, "swift": 2,
    "performance": 2, "latency": 2, "memory": 2, "cpu": 2, "profile": 2,
    "optimize": 2, "slow": 2, "bottleneck": 2,
    "wcag": 2, "accessibility": 2, "keyboard": 2, "contrast": 2,
}

# Small controlled translation map for common Turkish task objectives.
ALIASES = {"arayüz": "ui", "arayüzü": "ui", "arayüzdeki": "ui",
           "animasyon": "animation", "animasyonu": "animation",
           "sunucu": "server", "veritabanı": "database",
           "veritabanını": "database", "güvenlik": "security",
           "yetkilendirme": "authorization", "kimlik": "authentication",
           "dağıtım": "deployment", "dokümantasyon": "documentation",
           "belgelendirme": "documentation", "testler": "tests",
           "testleri": "tests", "testlerini": "tests",
           "araştır": "research", "araştırma": "research",
           "şema": "schema", "şemasını": "schema", "imaj": "image",
           "küçült": "optimize", "azalt": "optimize", "kayma": "overflow",
           "mobil": "mobile", "ödeme": "payment", "hata": "error",
           "durumları": "state", "uygula": "implement", "ekle": "add",
           "düzelt": "fix", "oluştur": "create", "yaz": "write",
           "güncelle": "update", "kapat": "fix", "iyileştir": "improve",
           "temizle": "clean", "styles": "style", "styling": "style",
           "animate": "animation", "duplicated": "duplication",
           "speed": "optimize", "yanıt": "response", "yanıtlarına": "response",
           "sayfalama": "pagination", "animasyonlu": "animation",
           "bildirim": "notification", "bileşeni": "component",
           "yapılandır": "configure", "doğrulayan": "verify",
           "yedeğini": "backup", "başlıkları": "header", "reranking": "rerank",
           "hata": "error", "hatalarını": "error", "döndür": "return",
           "yapılandırılmış": "structured", "menü": "menu",
           "menüdeki": "menu", "taşma": "overflow", "taşmayı": "overflow"}

MAX_SKILL_BYTES = 64 * 1024
CROSS_CUTTING = {"refactor", "performance"}
# A broad name token (api/ui/sql/test) is weaker evidence than a distinctive
# one (fastapi/postgres/playwright).
GENERIC_NAME_TOKENS = {"api", "ui", "ai", "sql", "test", "tests", "doc", "docs",
                       "app", "web", "write", "build", "create", "add", "fix",
                       "update", "make", "audit", "writer", "helper", "scout",
                       "automation", "profiler", "analyst", "engineer", "expert",
                       "tool", "library"}
ACTIONS = {
    "backend": {"add", "implement", "create", "expose", "fix", "refactor",
                "design", "build", "debug", "optimize", "validate", "handle",
                "support", "return", "serve", "extend", "update", "remove",
                "migrate", "speed"},
    "frontend": {"add", "implement", "create", "fix", "build", "design",
                 "style", "update", "make", "convert", "improve", "refactor",
                 "remove", "reduce", "prevent"},
    "animation": {"add", "create", "make", "animate", "improve", "reduce",
                  "fix", "smooth", "remove"},
    "security": {"audit", "scan", "harden", "prevent", "secure", "fix",
                 "check", "rotate", "add", "remove", "validate", "protect",
                 "escape", "verify", "configure"},
    "testing": {"write", "add", "fix", "increase", "run", "create", "update",
                "remove", "cover"},
    "devops": {"containerize", "deploy", "set", "configure", "fix", "add",
               "reduce", "optimize", "create", "update", "remove", "publish",
               "build", "provision", "trace", "prevent", "write"},
    "ai": {"build", "create", "add", "evaluate", "improve", "reduce",
           "implement", "update", "remove", "design", "test", "benchmark",
           "store"},
    "database": {"design", "write", "optimize", "fix", "model", "add",
                 "create", "update", "remove", "migrate", "index", "query",
                 "normalize", "resolve", "backfill", "partition", "rewrite"},
    "data": {"analyze", "clean", "transform", "visualize", "add", "create",
             "update", "remove", "build"},
    "docs": {"write", "update", "add", "create", "document", "improve",
             "remove", "fix"},
    "research": {"research", "investigate", "compare", "survey", "benchmark",
                 "evaluate", "explore", "assess", "review", "study"},
    "refactor": {"refactor", "restructure", "clean", "simplify", "extract",
                 "remove", "sort", "deduplicate"},
    "mobile": {"build", "create", "add", "fix", "implement", "update", "remove"},
    "performance": {"optimize", "improve", "reduce", "profile", "speed", "fix",
                    "measure", "prevent"},
    "accessibility": {"add", "fix", "improve", "audit", "update", "remove",
                       "build", "create", "make"},
}
DOMAIN_ORDER = ["backend", "frontend", "animation", "security", "testing",
                "devops", "ai", "database", "data", "docs", "research",
                "refactor", "mobile", "performance", "accessibility"]


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _raw_words(value: str) -> list[str]:
    if not isinstance(value, str):
        return []
    # Preserve common product names as single tokens before camel-case splitting.
    value = re.sub(r"\b(?:GitHub|GitLab|OpenAI|PostgreSQL|TypeScript|JavaScript|FastAPI|GraphQL|iOS)\b",
                   lambda match: match.group(0).lower(), value, flags=re.IGNORECASE)
    value = value.replace("İ", "i").replace("I", "i")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"[/_.+\-]", " ", value)
    return [w.casefold() for w in WORDS.findall(value)]


def tokens(value: str) -> set[str]:
    result = set()
    for word in _raw_words(value):
        word = ALIASES.get(word, word)
        if word in STOP or len(word) < 2:
            continue
        result.add(word)
        result.add(_stem(word))
    return result


def _canon(words: set[str]) -> set[str]:
    return {_stem(word) for word in words}


def _phrase_hits(value: str) -> list[tuple[str, dict[str, int]]]:
    words = [_stem(ALIASES.get(word, word)) for word in _raw_words(value)]
    text = " " + " ".join(words) + " "
    result = []
    for phrase, domains in PHRASES.items():
        needle = " " + " ".join(_stem(word) for word in phrase.split()) + " "
        if needle in text:
            result.append((phrase, domains))
    return result


def domain_scores(value: str) -> Counter:
    scores: Counter = Counter()
    for _phrase, mapping in _phrase_hits(value):
        for domain, weight in mapping.items():
            scores[domain] += weight
    for word in _raw_words(value):
        word = ALIASES.get(word, word)
        if word in STOP or len(word) < 2:
            continue
        for domain, terms in DOMAIN_TERMS.items():
            if word in terms or _stem(word) in terms:
                scores[domain] += TERM_WEIGHTS.get(word, 1)
    return scores


def domains(words: set[str]) -> set[str]:
    """Backward-compatible domain helper used by older callers/tests."""
    return {domain for domain, terms in DOMAIN_TERMS.items() if words & terms}


def _primary_domain(scores: Counter) -> str | None:
    if not scores:
        return None
    return max(DOMAIN_ORDER, key=lambda d: (scores.get(d, 0), -DOMAIN_ORDER.index(d)))


def skill_roots(project: Path, home: Path | None = None):
    home = home or Path.home()
    project = project.resolve()
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not path.is_dir():
            return
        seen.add(resolved)
        roots.append(path)

    for base in (project,):
        add(base / ".agents/skills")
        add(base / ".claude/skills")
        add(base / ".codex/skills")
        add(base / ".opencode/skills")
        add(base / ".cursor/skills")
        add(base / ".gemini/config/skills")
    # Ancestor skill directories are only considered inside a Git repository
    # (including linked worktrees, where .git is a file).
    chain: list[Path] = []
    cursor = project.parent
    found = False
    while cursor != cursor.parent:
        chain.append(cursor)
        if (cursor / ".git").exists():
            found = True
            break
        cursor = cursor.parent
    if found:
        for base in chain:
            add(base / ".agents/skills")
            add(base / ".claude/skills")
    for base in (home,):
        add(base / ".agents/skills")
        add(base / ".claude/skills")
        add(base / ".codex/skills")
        add(base / ".config/opencode/skills")
        add(base / ".cursor/skills")
        add(base / ".gemini/config/skills")
        add(base / ".gemini/antigravity-cli/skills")
    return roots


def _frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    lines = parts[1].splitlines()
    result: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            folded = value.startswith(">")
            block: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            result[key] = (" " if folded else "\n").join(x for x in block if x).strip()
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
        index += 1
    return result


def discover(project: Path, home: Path | None = None):
    home = home or Path.home()
    project = project.resolve()
    found: dict[str, dict] = {}
    seen_paths: set[Path] = set()
    for root in skill_roots(project, home):
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                real = path.resolve()
                stat = real.stat()
            except OSError:
                continue
            if not real.is_file() or stat.st_size > MAX_SKILL_BYTES:
                continue
            if path.is_symlink() or path.parent.is_symlink():
                # Symlinked skills are allowed only when the target stays in
                # the trusted project or home tree. Never read arbitrary paths.
                if not (real.is_relative_to(project) or real.is_relative_to(home.resolve())):
                    continue
            if real in seen_paths:
                continue
            try:
                content = real.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            meta = _frontmatter(content)
            name = meta.get("name", "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
                name = path.parent.name
            if name == "auto-orchestrator" or name in found:
                continue
            description = meta.get("description", "").strip()
            found[name] = {"name": name, "description": description[:300],
                           "path": str(real)}
            seen_paths.add(real)
    return sorted(found.values(), key=lambda x: x["name"])


def suggest_role(objective: str) -> str:
    """Advisory only; an explicitly assigned Supervisor role is never changed."""
    words = set(w.lower() for w in WORDS.findall(objective))
    if words & {"audit", "review", "reviewer", "inspect"}:
        return "reviewer"
    if words & {"test", "tests", "pytest", "unittest", "regression", "coverage", "e2e"}:
        return "tester"
    if words & {"research", "investigate", "explore", "survey", "benchmark", "araştırma"}:
        return "explorer"
    return "implementer"


def route(task: dict, skills: list[dict], max_skills: int = 2):
    """Deterministic, abstaining router; metadata is data, never instructions."""
    if type(max_skills) is not int or not 0 <= max_skills <= 5:
        raise SupervisorError("max_skills out of range")
    if max_skills == 0:
        return []
    objective = task.get("objective", "")
    read = task.get("read", []) or []
    write = task.get("write", []) or []
    if not isinstance(read, list) or not isinstance(write, list):
        raise SupervisorError("read/write scopes must be lists")
    scope_text = " ".join(str(x) for x in read + write)
    objective_tokens = tokens(objective)
    scope_tokens = tokens(scope_text)
    task_domains = domain_scores(objective)
    action_words: set[str] = set()
    raw_actions = [ALIASES.get(word, word) for word in _raw_words(objective)]
    for word in raw_actions:
        action_words.add(word)
        action_words.add(_stem(word))
    # Use the imperative verb, not every noun that happens to share a domain
    # term ("Delete the benchmark folder" is not a benchmark task).
    action_verbs = set()
    if raw_actions:
        for word in (raw_actions[0], raw_actions[-1]):
            action_verbs.add(word)
            action_verbs.add(_stem(word))
    domain_action_ok = any(action_verbs & ACTIONS.get(domain, set()) for domain in task_domains)
    explicit = task.get("skills", [])
    if not isinstance(explicit, list) or any(not isinstance(x, str) for x in explicit):
        raise SupervisorError("Explicit skills must be a list of names")
    selected = set(explicit)
    if not skills:
        return []

    # Rare description terms are stronger evidence than common ones.
    doc_freq: Counter = Counter()
    profiles = []
    for item in skills:
        name = item["name"]
        description = item.get("description", "")
        name_words = tokens(name)
        desc_words = tokens(description)
        name_canon = _canon(name_words)
        desc_canon = _canon(desc_words)
        skill_domains = domain_scores(name + " " + description)
        for word in desc_canon:
            doc_freq[word] += 1
        profiles.append((item, name_canon, desc_canon, skill_domains))
    total = max(1, len(skills))

    ranked = []
    for item, name_words, desc_words, skill_domains in profiles:
        name = item["name"]
        manual = name in selected
        if name == "generic-helper" and not manual:
            continue
        primary = _primary_domain(skill_domains)
        shared = set(task_domains) & set(skill_domains)
        objective_canon = _canon(objective_tokens)
        name_hits = objective_canon & name_words
        desc_hits = objective_canon & desc_words
        scope_hits = _canon(scope_tokens) & (name_words | desc_words)
        desc_weight = sum(1 + min(1.5, math.log((total + 1) / (doc_freq.get(word, 1) + 1)))
                          for word in desc_hits)
        domain_overlap = sum(min(task_domains[d], skill_domains[d]) for d in shared)
        name_weight = sum(3 if word in GENERIC_NAME_TOKENS else 8 for word in name_hits)
        score = name_weight + 3 * desc_weight + 4 * domain_overlap + len(scope_hits)
        if manual:
            score += 100
        elif primary == "refactor" and any(d != "refactor" and task_domains[d] >= 2 for d in task_domains):
            # A refactor skill must not outrank a domain-specific skill just
            # because its name echoes the verb.
            score *= 0.4
        elif primary == "performance" and any(d not in CROSS_CUTTING and task_domains[d] >= 2 for d in task_domains):
            score *= 0.9
        strong_lexical = any(word not in GENERIC_NAME_TOKENS for word in name_hits) or len(desc_hits) >= 2
        action_ok = (not task_domains or (primary in task_domains and
                     bool(action_verbs & ACTIONS.get(primary or "", set()))))
        if not manual:
            if task_domains and not action_ok and not strong_lexical:
                continue
            if not task_domains and not strong_lexical:
                continue
            if task_domains and primary not in task_domains and not strong_lexical:
                continue
            if task_domains and shared and not strong_lexical and max(task_domains.values()) < 2:
                # A single broad word (e.g. "json") is not enough for a
                # domain-only fallback; require a specific term or phrase.
                continue
            if score < 4:
                continue
        ranked.append((score, item, primary))

    ranked.sort(key=lambda x: (-x[0], x[1]["name"]))
    result = []
    used_primary: set[str] = set()
    for score, item, primary in ranked:
        if item["name"] in selected:
            result.append(item)
            used_primary.add(primary or "")
            continue
        if primary and primary in used_primary:
            continue
        result.append(item)
        used_primary.add(primary or "")
        if len(result) >= max_skills:
            break
    return result[:max_skills]


def brief(project: Path, plan: dict, task_id: str, home: Path | None = None,
          max_skills: int = 2):
    tasks, _ = validate_plan(plan)
    if task_id not in tasks:
        raise SupervisorError("Task not in plan")
    task = tasks[task_id]
    original = next(x for x in plan["tasks"] if x["id"] == task_id)
    skills = route(task | {"skills": original.get("skills", [])},
                   discover(project, home), max_skills)
    task_domains = domain_scores(task["objective"])
    return {
        "task_id": task_id, "role": task["role"], "objective": task["objective"],
        "routing": {"version": 3, "domains": sorted(task_domains),
                    "suggested_role": suggest_role(task["objective"]),
                    "abstained": not bool(skills)},
        "depends_on": task["depends_on"], "read_scope": task["read"],
        "write_scope": task["write"],
        "selected_skills": [{"name": s["name"], "path": s["path"]} for s in skills],
        "required_result": {
            "summary": "concise completed work", "changed_files": [],
            "tests": [{"name": "test command or check", "status": "passed|failed|skipped"}],
            "findings": []},
        "rules": [
            "Read only the selected skills needed for this bounded task.",
            "Do not access or write outside assigned scopes; no unrelated refactors.",
            "Don't claim native task completion without a successful native tool result.",
            "Return concrete test evidence and do not fabricate measurements.",
            "Never include secrets or whole chat transcripts in the brief or output."],
    }



def delegation_decision(objective: str) -> dict:
    """Advisory only. Native IDE/model retains all dispatch decisions.

    Bias toward a single agent: V6.2 measured negative gains for tiny work.
    Count explicitly enumerated independent tasks, never assume word count alone
    implies that parallel *writing* is safe.
    """
    if not isinstance(objective, str):
        raise SupervisorError("objective must be a string")
    text = objective.strip()
    if not text:
        return {"mode": "single", "max_agents": 1, "reason": "empty"}
    numbered = re.findall(r"(?m)^\\s*(?:[-*]\\s+|[1-9][0-9]?[.)]\\s+)\\S", text)
    marked = bool(re.search(r"(?i)\\b(parallel|independent|independently|concurrent|paralel|bağımsız|eşzamanlı)\\b", text))
    files = set(re.findall(r"(?i)\\b[\\w.-]+/([\\w.-]+/[\\w.-]+)\\b", text))
    tasks = len(numbered)
    if marked and tasks >= 2:
        return {"mode": "consider-parallel", "max_agents": min(3, tasks), "reason": "explicit-independent-items"}
    if marked and len(files) >= 2:
        return {"mode": "consider-parallel", "max_agents": min(3, len(files)), "reason": "explicit-separated-paths"}
    return {"mode": "single", "max_agents": 1, "reason": "no-proven-independent-work"}


def select_prompt(payload: dict) -> dict:
    """Route one in-memory prompt against an IDE-provided catalog.

    Input/output do not include raw task text in the returned JSON. This call
    never opens files, creates processes, or persists prompts.
    """
    if not isinstance(payload, dict):
        raise SupervisorError("select input must be an object")
    objective = payload.get("objective")
    catalog = payload.get("skills")
    if not isinstance(objective, str) or len(objective) > 12000:
        raise SupervisorError("objective must be a string up to 12000 characters")
    if not isinstance(catalog, list) or len(catalog) > 128:
        raise SupervisorError("skill catalog must be <= 128 items")
    safe = []
    for item in catalog:
        if not isinstance(item, dict):
            raise SupervisorError("invalid catalog entry")
        name, description = item.get("name"), item.get("description")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name):
            raise SupervisorError("invalid catalog skill name")
        if not isinstance(description, str) or len(description) > 500:
            raise SupervisorError("invalid skill description")
        safe.append({"name": name, "description": description, "path": ""})
    ranked = route({"objective": objective, "role": "implementer",
                    "read": [], "write": [], "skills": []}, safe,
                   max_skills=2)
    return {"version": 1, "selected": [x["name"] for x in ranked],
            "delegation": delegation_decision(objective),
            "abstained": not bool(ranked)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    sub.add_parser("select", help="Read one JSON object from stdin; return skill names and delegation advice")
    brief_parser = sub.add_parser("brief")
    brief_parser.add_argument("--plan", type=Path, required=True)
    brief_parser.add_argument("--task-id", required=True)
    brief_parser.add_argument("--max-skills", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            result = discover(args.root)
        elif args.command == "select":
            import sys
            body = sys.stdin.buffer.read(100001)
            if len(body) > 100000:
                raise SupervisorError("select payload too large")
            result = select_prompt(json.loads(body.decode("utf-8")))
        else:
            result = brief(args.root, json.loads(args.plan.read_text(encoding="utf-8")),
                           args.task_id, max_skills=args.max_skills)
        print(json.dumps(result, indent=2))
    except (SupervisorError, OSError, ValueError) as exc:
        parser.exit(1, "ROUTER ERROR: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
