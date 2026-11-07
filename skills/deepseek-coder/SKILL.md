---
name: deepseek-coder
description: >
  Multi-Agent Orchestration Skill: Gemini acts as the Master Orchestrator / Planner
  while delegating all heavy coding, quantitative math, and implementation tasks to multiple
  concurrent DeepSeek V4.1 Flash subagents running in parallel (via Evren / OpenCode gateway).
  Use whenever coding, algorithm writing, refactoring, or parallel implementation is needed.
license: MIT
---

# DeepSeek Multi-Agent Parallel Execution Skill

## Güncel kullanım (2026-11-08) — önce ajan, sonra script

1. **Ajanlı işler (dosya değiştirme, komut, test):** curl/script gerekmez.
   ```sh
   opencode run --model opencode-go/deepseek-v4.1-flash#max --auto \
     -f <brief.md> "<talimat>"
   ```
   Gerçek ajandır: dosya okur/yazar, komut çalıştırır, kendi doğrulamasını yapar.
   Doğrulandı: geçici bir dosya üzerinde çalıştırıldı, kod doğru düzenlendi.

2. **Codex içinde:** native provider (`-c model_provider=opencode-go -c model=deepseek-v4.1-flash`)
   veya `opencode run`. Oturum Codex uygulamasında sohbet olarak görünür.

3. **Batch/toplu metin üretimi:** bu skill'in `scripts/deepseek.py` aracı
   (`--batch tasks.json --workers 4`). Kod çalıştırmaz; dosyaya metin yazar.

4. **Curl:** yalnızca tek soruluk metin işleri için (`deepseek-ask.sh`).

### Anahtar ve gateway

- `OPENCODE_API_KEY`: `~/Desktop/trading-system/tlab/.env` veya
  `~/Desktop/english-work/.env` (aynı anahtar). **Script içine gömülü anahtar
  kaldırıldı** — env ya da dosyadan okunur.
- Birincil: Evren (`https://evren-llmapi.ssyz.org.tr/v1`, `~/.config/opencode/evren.key`)
- Yedek: OpenCode Zen Go (`https://opencode.ai/zen/go/v1`, `x-opencode-session` header zorunlu)


## Core Architectural Principle

This skill enforces a strict separation of concerns between **Strategic Orchestration** and **Code Execution**:

```
                  ┌────────────────────────────────────────┐
                  │       GEMINI (MASTER ORCHESTRATOR)     │
                  │   - Strategic Planning & Next Steps    │
                  │   - Task Decomposition & Scoping       │
                  │   - Quality Audit & Validation Gate    │
                  └──────────────────┬─────────────────────┘
                                     │
           ┌─────────────────────────┼─────────────────────────┐
           │ (Parallel Dispatches)   │                         │
           ▼                         ▼                         ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│ DEEPSEEK WORKER #1  │   │ DEEPSEEK WORKER #2  │   │ DEEPSEEK WORKER #3  │
│ Component / Logic A │   │ Component / Logic B │   │ Verification / Unit │
│ (Evren / OpenCode)  │   │ (Evren / OpenCode)  │   │ (Evren / OpenCode)  │
└─────────────────────┘   └─────────────────────┘   └─────────────────────┘
```

### 1. Gemini Role: Master Orchestrator & Architect
* Gemini **never** wastes turns or context manually writing large blocks of boilerplate or algorithmic code.
* Gemini is the **Orchestrator**:
  - Analyzes user objectives and formulates the multi-step roadmap.
  - **Wave-0 Total Parallelism (Zero-Wait Rule):** Immediately identifies all decoupled workstreams (e.g., `mobile/` scaffolding & package downloads, UI/UX design proposals, `docs/`, `frontend/`, and `backend/` `dev`/`local` Dockerization) and launches them **simultaneously in Wave 0**. Never wait for backend/Docker infrastructure refactoring to finish before starting mobile project setup, dependency downloads, or UI design.
  - Determines the next immediate actions and defines the acceptance criteria.
  - Decomposes non-trivial problems into non-overlapping, independent subtasks.
  - Dispatches parallel execution to multiple DeepSeek subagent workers (`reasoning_effort="max"`).
  - Validates and integrates the generated artifacts and reports progress to the user.

### 2. DeepSeek Role: Parallel Execution Subagent Workers
* **DeepSeek V4.1 Flash** (powered by Evren with automatic OpenCode fallback) is the primary code generator, refactorer, and quantitative math engine.
* Operates in **max reasoning mode** (`reasoning_effort="max"`), producing bug-free, production-grade implementations.
* **UI/UX Design Engineering Integration (Mandatory for UI Tasks):**
  - Whenever designing or implementing UI/UX (web or mobile), workers MUST read and apply the UI design skills in `/Users/eniskorkut/.agents/skills/` (`better-ui`, `apple-design`, `mobile-native`, `emil-design-eng`, `animate`) and `/Users/eniskorkut/.gemini/antigravity/builtin/skills/generative_ui/SKILL.md`.
* **Parallel Execution & Non-Blocking Mindset (Mandatory):**
  - Independent work MUST ALWAYS be executed concurrently in parallel to maximize velocity and minimize wall-clock time. Never serialize independent jobs when they can run side-by-side.
  - **Zero-Wait Decoupled Tracks:** Never block unrelated workstreams on each other. For example, mobile scaffolding/package setup (`mobile/`), frontend components, documentation, or test suites that do not depend on backend `dev`/`local` infrastructure separation must start immediately in parallel rather than waiting in a sequential queue.
  - Identify decoupled tracks at the very start of every request and launch their workers simultaneously.
* **Container & Dependency CVE Security Gate (Mandatory):**
  - Whenever Dockerfiles, container images, or package manifests are created or updated, always audit base images and built containers for known CVE vulnerabilities (via OSV API `https://api.osv.dev/v1/querybatch`, `docker scout cves`, `trivy`, `pip-audit`, or `npm audit`) in parallel with integration tests.
  - Always include `apk upgrade --no-cache` (on Alpine images like `caddy:2-alpine`, `node:20-alpine`) or `apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*` (on Debian `-slim` images like `python:3.12-slim`) inside Dockerfiles (`Dockerfile.dev`, `Dockerfile.local`) to patch OS-level CVEs (e.g., `zlib`, `musl`, `util-linux`, `tar`).

---

## Tooling & Execution

The skill provides an executable CLI helper at:
`/Users/eniskorkut/.gemini/config/skills/deepseek-coder/scripts/deepseek.py`

### 1. Parallel Multi-Worker Batch Execution (Preferred)

To run multiple tasks simultaneously in parallel:
Create a JSON task list and dispatch with `--batch`:

```json
[
  {
    "id": "worker-data-etl",
    "prompt": "Implement the DuckDB data loader and caching layer in tlab/src/tlab/quant/loader.py",
    "output": "tlab/src/tlab/quant/loader.py",
    "extract_code": true
  },
  {
    "id": "worker-indicators",
    "prompt": "Implement vectorised ATR, Bollinger and Momentum indicators in tlab/src/tlab/quant/indicators.py",
    "output": "tlab/src/tlab/quant/indicators.py",
    "extract_code": true
  },
  {
    "id": "worker-unit-tests",
    "prompt": "Write comprehensive pytest unit tests for the loader and indicators in tlab/tests/test_components.py",
    "output": "tlab/tests/test_components.py",
    "extract_code": true
  }
]
```

Run concurrently with 4 parallel threads:
```bash
/Users/eniskorkut/.gemini/config/skills/deepseek-coder/scripts/deepseek.py \
  --batch tasks.json \
  --workers 4
```

### 2. Single Task Asynchronous Dispatch

For a single bounded task:
```bash
/Users/eniskorkut/.gemini/config/skills/deepseek-coder/scripts/deepseek.py \
  --prompt "Refactor this code to fix QuantConnect API bugs" \
  --file path/to/source.py \
  --extract-code \
  --output path/to/destination.py
```

### 3. Native Antigravity Subagent Integration
Gemini can also launch DeepSeek workers in background tasks (`run_command` with background tracking) so Gemini remains free to monitor, plan subsequent phases, or interact with the user without blocking.

---

## Gateways & Resiliency
1. **Primary Gateway:** Evren (`https://evren-llmapi.ssyz.org.tr/v1`) using key at `~/.config/opencode/evren.key`.
2. **Secondary Gateway (Fallback):** OpenCode Zen Go (`https://opencode.ai/zen/go/v1`) using `OPENCODE_API_KEY` from `tlab/.env`.
3. If Evren experiences high traffic or timeouts (>150s), the runner automatically fails over to OpenCode without crashing.
