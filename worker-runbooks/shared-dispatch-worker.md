# Shared Dispatch Worker Policy

These rules apply to all Saffron isolated worker sessions regardless of lane.

---

## Dispatch v0.3 Source of Truth

- Use `DISPATCH_URL` and `DISPATCH_AGENT_TOKEN` only. Do NOT use `MISSION_CONTROL_*`.
- GitHub issues/PRs are the user-facing source of truth; Dispatch owns queues, claims, leases, checkpoints, lane assignment, and status transitions.
- GitHub Projects are deprecated; do not read or mutate project boards.
- Direct GitHub status/agent label edits are avoided when Dispatch APIs exist.

---

## Five-Column Board Semantics

| Status | Meaning |
|---|---|
| `status/backlog` | Needs triage/grooming; not yet ready for agents |
| `status/ready` | Groomed/actionable; normal queue source |
| `status/in-progress` | Claimed/implementation started |
| `status/in-review` | PR opened/checks/review pending; issue still open |
| `status/done` | GitHub issue closed/terminal only |

**Hard rule:** Opening or updating a PR is not Done. An open issue with an unmerged PR must be `status/in-review`, not `status/done`.

---

## Work Selection

- Prefer Dispatch queue/API over manually scraping GitHub labels.
- Pick `status/ready` work by default.
- Do not pick `status/backlog` unless explicitly asked.
- PR-fix queue takes precedence over new issue work.
- One item per run. One bounded step per run.
- Respect `nextAction` / `checkpoint` if active work exists.

---

## Preflight Before Claim

Before claiming work, verify all of:
- Dispatch is reachable.
- token is valid.
- repo workspace can be prepared (`/data/git/{repo}`).
- required tools are available.
- checkpoint/status reporting will succeed after the bounded step.

If any check FAILS after claiming:
- release/mark blocked through Dispatch API if available.
- report clear stuck reason.
- do not silently leave ghost claimed work.

If a run cannot proceed after claiming: END with `Stuck: {reason}`.

---

## Active Work Resumption

```bash
curl -fsS "$DISPATCH_URL/api/agents/saffron/active-work"
```

If Dispatch returns active work with `checkpoint` / `nextAction`:
1. **Lane verification before obeying.** Inspect the lane field on the active work. If the lane matches the worker's lane, proceed. If the lane cannot be verified or does not match, stop with `Stuck: active work lane mismatch or could not be verified.` Do not mutate Dispatch state for mismatched active work.
2. Obey `nextAction` exactly.
3. Perform one bounded step.
4. Update Dispatch via `/api/agent-work/checkpoint` or `/api/agent-work/finish`.
5. STOP.

Do not infer a different workflow from memory or old prompt text.

---

## Claiming

Claim unclaimed work through Dispatch before starting.
Only skip if the selected item already has `agent/saffron` in its agent field or active-work context.

```bash
curl -fsS -X POST "$DISPATCH_URL/api/issues/claim" \
  -H "Authorization: Bearer $DISPATCH_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"issueId":"{issueId}","repoFullName":"{repo}","issueNumber":{number},"agentName":"saffron"}'
```

If claim fails: END: `Stuck: claim failed for {repo} #{number}: {reason}.`

---

## PR Rules

- Before coding, check for an existing PR. Do NOT open a duplicate.
- PR body MUST start with exactly one of:
  - `Fixes #{number}` when the PR fully satisfies the in-scope issue.
  - `Refs #{number}` when the PR is partial/incremental.
  - Nothing before that keyword: no heading, no blank line.
- After opening/updating a PR, set/checkpoint through Dispatch so the issue is In Review. Do not mark open issues Done.

---

## Renovate Issues

Excluded from Dispatch queues unless the queue item explicitly says Renovate work is requested.

---

## Final Response Formats

END with exactly one of:
- `Pipeline is clear.` / `Escalated lane is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Done. Decomposed {repo} #{number}: {child_urls}.`
- `Stuck: {reason}.`

**Hard completion gate:** Do not end after local commit only. After any PR interaction: push, create/update PR, verify with `gh pr view`, update Dispatch checkpoint/status.
