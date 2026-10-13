# Delegation contract

Use the following structure internally per task (not necessarily as on-disk YAML):

- `task_id`: opaque and unique within the run
- `goal`: concrete acceptance criteria
- `depends_on`: IDs of prerequisite tasks
- `read_scope`: allowed code areas
- `write_scope`: explicit file ownership; empty for read-only tasks
- `verification`: commands / evidence expected
- `output`: changed files, tests, issues, uncertainty

**Planning**: Draw a directed acyclic graph of tasks; batch ready nodes with no unresolved dependencies. Prefer exploration in parallel; only allow multiple writing jobs if edits and contracts are independent or isolated by native worktree support. Stop and ask permission for destructive actions.

**Integration**: Main agent owns merges, conflicts, and user-facing response. Subagent results are evidence, not automatically validated truth. Never let a subagent recursively spawn more agents by default.
