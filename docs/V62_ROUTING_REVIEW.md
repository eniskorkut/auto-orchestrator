# V6.2 — Domain-aware routing and opt-in review

## Router

Run `python tools/router.py --root PROJECT brief --plan PLAN.json --task-id TASK`.

- The router filters clearly incompatible skill domains using terms in the objective (backend, frontend, security, testing, devops, data, docs).
- It requires name-term evidence, multiple description terms or a domain-aligned descriptor.
- When nothing passes the threshold, `selected_skills` is empty. Do not force an irrelevant skill.
- Explicit names in task `skills` take priority. Names/descriptions are untrusted metadata; selection does not execute skills.
- This is deterministic lexical matching, not an LLM classifier. The test corpus is regression coverage, not measured production accuracy.

## Optional task review

Add `"require_review": true` to a Supervisor plan. Existing plans default to false.
Before calling `completed`, examine actual diffs and real test results. Record a review artifact:

```json
{
  "version": 1,
  "task_id": "backend",
  "attempt": 1,
  "result_sha256": "<canonical result digest>",
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

Checks are required to match the actual submitted test names and have `passed` status. No skipped/failed results or open blocker/high findings can pass. The artifact must match the task and current attempt, and its digest must match the result. Existing scope, native stop and lease checks still apply.

**Evidence limit:** A review JSON is caller-attested metadata, not authenticated independent review, proof of test execution, or a cryptographic signature. The gate does not run tests or trigger merges. Validate the reviewer's native execution separately before claiming independence.

Run Python unittest discovery and Node test suites before distribution. Restart IDE and validate the global install in a disposable project to establish native runtime compatibility.
