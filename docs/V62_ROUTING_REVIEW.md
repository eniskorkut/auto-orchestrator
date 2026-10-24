# V6.2 — Domain-aware routing and opt-in review

## Router

Run `python tools/router.py --root PROJECT brief --plan PLAN.json --task-id TASK`.

- The router filters clearly incompatible skill domains using terms in the objective (backend, frontend, security, testing, devops, data, docs).
- It requires name-term evidence, multiple description terms or a domain-aligned descriptor.
- When nothing passes the threshold, `selected_skills` is empty. Do not force an irrelevant skill.
- Explicit names in task `skills` take priority. Names/descriptions are untrusted metadata; selection does not execute skills.
- `brief` includes `routing.domains`, `routing.suggested_role`, and `routing.abstained`. The role is advisory; the Supervisor task's declared role is never silently changed.
- Routing v3 uses domain terms, multi-word phrases, imperative-verb intent, brand/plural normalization, and a controlled Turkish alias map. It remains deterministic lexical matching, not an LLM classifier.
- Skill discovery covers project `.agents`, `.claude`, `.codex`, `.opencode`, `.cursor` and `.gemini` roots, ancestor `.agents`/`.claude` folders inside a Git worktree, and home `.agents`/`.claude`/`.codex`/opencode/cursor/gemini roots. Symlinked skills are accepted only when their resolved target stays inside the project or home tree. Multi-line YAML descriptions are parsed.
- `tests/data/router_goldens_v62.json` is a labelled regression corpus with dev/eval/holdout/blind splits; `tests/test_router_quality_v62.py` enforces the 90% Top-1 and 5% false-positive targets on the eval split. A fresh unseen corpus can still expose long-tail vocabulary gaps; the router abstains rather than guessing when evidence is weak. It does not claim production accuracy.

## Optional task review

Add `"require_review": true` to a Supervisor plan. Existing plans default to false.
Before calling `completed`, examine actual diffs and real test results. Record a review artifact:

```json
{
  "version": 1,
  "task_id": "backend",
  "attempt": 1,
  "result_sha256": "<canonical result digest>",
  "code_sha256": "<digest of the changed files at review time>",
  "verdict": "approved",
  "checks": [{"name": "pytest", "status": "passed"}],
  "findings": [{"severity": "low", "status": "open", "summary": "Consider docs follow-up"}]
}
```

Get `result_sha256` using `tools.review_gate.result_digest(result)` against the exact JSON result object.

```sh
python tools/supervisor.py --root PROJECT completed --run-id RUN --task-id backend \
  --lease REAL_LEASE --result result.json --review review.json
```

Checks are required to match the actual submitted test names and have `passed` status. No skipped/failed results or open blocker/high findings can pass. The artifact must match the task and current attempt, and its digest must match the result. Existing scope, native stop and lease checks still apply. A successful attestation stores only `review_attested` and `review_digest` in Supervisor status, not the raw review artifact.

**Evidence limit:** A review JSON is caller-attested metadata, not authenticated independent review, proof of test execution, or a cryptographic signature. The gate does not run tests or trigger merges. The code digest prevents stale reuse after file changes, but it does not prove that a different agent performed the review. Validate the reviewer's native execution separately before claiming independence; never label a caller-attested review as `independently_verified`.

Run Python unittest discovery and Node test suites before distribution. Restart IDE and validate the global install in a disposable project to establish native runtime compatibility.
