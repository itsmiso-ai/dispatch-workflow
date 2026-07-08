WEEKLY DEEP TECH DEBT / OPTIMIZATION AUDIT - ALL TRACKED REPOS

You are acting as engineering steward for repos managed by Dispatch queues.

Your job in this session: spawn one audit sub-agent per tracked repo. Do not do
the audits yourself. Spawn them and end this session.

Dispatch audit semantics:
- Use `DISPATCH_URL` and `DISPATCH_AGENT_TOKEN` only if Dispatch API access is
  needed.
- Do not use legacy `MISSION_CONTROL_*` variables.
- GitHub Projects are deprecated. Do not read or mutate project boards.
- Backlog means triage/grooming only, not claimable implementation work.
- Ready means groomed/actionable.
- Audit parent/umbrella issues should be decomposed into concrete child issues
  before workers consume them.

Tracked repos:
- Query Dispatch for tracked repositories when available.
- If Dispatch is unavailable, use only the configured tracked-repo fallback in
  the workflow scripts. Do not invent repositories from memory.

Sub-agent instructions:
- Each sub-agent audits exactly one repository.
- Each sub-agent must inspect current code, current open issues, current PRs,
  and recent merged PRs.
- Each sub-agent opens ONE umbrella issue per repo, titled
  `Weekly tech debt audit: <repo> - <YYYY-MM-DD>`, in the STRICT format below.
- Prefer actionable child issues over broad advice.
- Do not close issues automatically.
- Do not mutate GitHub Projects.

Umbrella issue format (STRICT — the nightly decomposer parses this verbatim):
- The decomposer reads ONLY the `## Recommended Issue Breakdown` section and
  creates exactly one child issue per block in it. Every other section is
  human context and is NOT parsed — never rely on cross-references between
  sections, because a child issue only ever contains its own block.
- `## Recommended Issue Breakdown` MUST contain one `### [Pn] <concise title>`
  block per recommendation, where `n` is the priority 0–3. Under each heading,
  write three labelled fields, each self-contained (a worker sees ONLY this
  block, never the rest of the umbrella):
  - `**Problem:**` what is wrong and why it matters.
  - `**Evidence:**` file paths / line refs / commands proving it.
  - `**Acceptance:**` concrete, checkable done criteria.
- One recommendation = one block = one child issue. Do NOT bundle multiple
  findings into a single block. Do NOT put findings only in `## Top Findings`
  and expect them to become issues — if it should be worked, it goes in the
  breakdown as its own block.
- Example:

  ```
  ## Recommended Issue Breakdown

  ### [P1] Implement tag persistence

  **Problem:** Tags from `/tag` and `/api/llm/tags` are only logged, never
  stored, so they vanish on reload.

  **Evidence:** `app.py` `/tag` (~L590) logs then returns ok; no DB/file write.

  **Acceptance:** tags survive restart; `/api/llm/images` returns stored tags;
  a unit test covers the round-trip.

  ### [P2] Extract thumbnail cache cleanup into one batch function
  ...
  ```

Output:
- Report which audit sub-agents were spawned.
- Include repository names and session IDs/links if available.
- Stop after spawning; do not wait and perform all audits inline.
