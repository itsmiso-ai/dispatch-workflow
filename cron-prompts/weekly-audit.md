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
- Each sub-agent should produce concrete findings with severity, evidence,
  rationale, and recommended issue breakdown.
- Prefer actionable child issues over broad advice.
- Do not close issues automatically.
- Do not mutate GitHub Projects.

Output:
- Report which audit sub-agents were spawned.
- Include repository names and session IDs/links if available.
- Stop after spawning; do not wait and perform all audits inline.
