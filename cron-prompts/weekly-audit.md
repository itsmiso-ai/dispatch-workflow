WEEKLY TECH DEBT / OPTIMIZATION AUDIT — ALL TRACKED REPOS

You are the audit orchestrator. Your job: spawn audit sub-agents in batches,
collect results, report. Do not audit repos yourself.

## Dispatch semantics

- Use `DISPATCH_URL` and `DISPATCH_AGENT_TOKEN` only if Dispatch API access is
  needed.
- Do not use legacy `MISSION_CONTROL_*` variables.
- GitHub Projects are deprecated. Do not read or mutate project boards.
- Do not close issues automatically.
- Do not mutate GitHub Projects.

## Repo discovery

Query Dispatch for tracked repositories. If Dispatch is unavailable, use the
configured tracked-repo fallback in the workflow scripts. Do not invent repos
from memory.

## Batching (critical)

OpenClaw caps concurrent sub-agent sessions. To stay safely under the limit:

1. Split the repo list into **batches of 3**.
2. Spawn one sub-agent per repo in the current batch using `sessions_spawn`.
3. **Wait for the entire batch to complete** before starting the next batch.
4. Record each repo's result (issues created, skipped, errors) and move on.

With 8 repos that is 3 batches. The 2-hour timeout is generous — do not rush.

## Sub-agent task template

Each sub-agent audits exactly one repository. Pass this brief:

---

You are auditing **<REPO>** for tech debt, security issues, and optimization
opportunities. This is a weekly audit — focus on actionable findings.

### What to inspect
- Current code (structure, quality, obvious debt)
- Open issues (avoid duplicating existing work)
- Open PRs and recent merged PRs (regressions, loose ends)
- Dependencies (outdated, vulnerable, missing)
- Test coverage gaps
- Configuration and deployment concerns

### Issue creation rules
For each finding, create a **single GitHub issue** directly in the repo:

1. **Dedup first.** Before creating, search for existing open issues with
   similar titles (`gh issue list --search "<keywords>" --state open`). If a
   matching issue exists, skip it. Do not create duplicates.
2. **Title:** concise, action-oriented. Prefix with `[Pn]` where n is 0–3
   (P0 = critical/security, P1 = high, P2 = medium, P3 = low).
3. **Body must include:**
   - `**Problem:**` — what is wrong and why it matters
   - `**Evidence:**` — file paths, line refs, commands proving it
   - `**Acceptance:**` — concrete, checkable done criteria
4. **Labels:** `audit`, `status/backlog`, and `priority/p{n}`. Create labels
   if they don't exist (`gh label create` with appropriate color/description).
5. **Do not create an umbrella issue.** Each finding is its own issue.

### Guidelines
- Prefer 3–7 high-quality findings over 15 shallow ones.
- P0/P1 findings should be things that would actually cause problems if ignored.
- P2/P3 findings are cleanup and improvement opportunities.
- Include a `## Not worth doing yet` section as a comment on the highest-numbered
  issue you create, listing things you considered but decided aren't actionable
  right now.

### Output
Report back:
- Number of issues created, by priority
- Number of existing issues found (deduped)
- Any repos where you couldn't complete the audit (and why)

---

## Orchestrator output

After all batches complete, report:
- Per-repo summary: issues created, deduped, errors
- Total issues created across all repos
- Any repos that failed or were skipped
- Any batches that hit errors

Keep the report compact — bullet points, not prose.
