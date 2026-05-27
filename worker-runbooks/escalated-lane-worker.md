# Escalated-Lane Worker — Dispatch Queue Consumption

Lane: `escalated`. This worker is identified as `$DISPATCH_AGENT_NAME=saffron-escalated`.

---

## Active-Work Lane Verification

Before obeying Dispatch active-work `nextAction` / `checkpoint`:

1. Inspect the active work's lane field.
2. If the lane is `escalated` → proceed with step 2 of the active-work resumption flow.
3. If the lane is `normal` or cannot be verified → stop. END: `Stuck: active work lane mismatch or could not be verified.`

Do not silently discard mismatched active work. Do not mutate Dispatch state for mismatched active work.

---

## PR-Fix Queue (Precedence)

Check deterministic preflight before picking new work:

```bash
DISPATCH_AGENT_NAME=saffron-escalated python3 /home/node/.openclaw/workspace-saffron/scripts/dispatch_worker_preflight.py --lane escalated --claim --json
```

Interpret the returned action before doing anything else:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: handle the returned PR-fix item only.
- `resume-active-work`: obey the returned `nextAction` exactly.
- `claim-ready-issue`: implement the returned claimed Ready issue.

The legacy direct PR-fix queue command is:

```bash
DISPATCH_AGENT_NAME=saffron-escalated python3 /home/node/.openclaw/workspace-saffron/scripts/pr_fix_queue.py next --lane escalated
```

If it prints a JSON PR-fix item, handle the existing PR only:
1. Verify PR is open and authored by `itsmiso-ai`.
2. Verify head owner is `misospace` or `joryirving`.
3. Fetch repo under `/data/git/{repo-name}`, checkout the queued branch, pull/rebase.
4. Read PR comments/reviews/check failures.
5. Apply the smallest requested fix, validate, commit if needed, push to the SAME branch.
6. Comment on the PR with what changed and validation.
7. Mark queue item fixed/stale/blocked with `pr_fix_queue.py mark ...`.
8. END: `Done. PR #{pr} updated for {repo}: {pr_url}.` or `Stuck: {reason}.`

Workers must NOT open a new PR for queued PR-fix work.

---

## Select Ready Escalated Work

Prefer the deterministic preflight packet over manually reading the queue.
Manual queue inspection is only for debugging the preflight result:

```bash
curl -fsS "$DISPATCH_URL/api/agents/$DISPATCH_AGENT_NAME/queue?lane=escalated"
```

Select exactly one actionable item:
- Prefer the first item already claimed by `agent/$DISPATCH_AGENT_NAME` if Dispatch returns it.
- Otherwise choose the first unclaimed `status/ready` escalated item.
- Do NOT choose `status/backlog`; Backlog is triage only.
- Ignore decomposed audit parents and items claimed by other agents.
- Do NOT choose Renovate issues unless explicitly requested.

If no $DISPATCH_AGENT_NAME-claimed or unclaimed claimable Ready escalated work exists: END: `Escalated lane is clear.`

---

## Valid Escalated Actions

Work exactly one bounded unit per run. Valid actions:
- Decompose a broad audit/umbrella issue into concrete child GitHub issues, then call Dispatch decomposition/checkpoint APIs.
- Implement one focused high-impact fix and open/update a PR.
- Write one concrete design/RFC comment when implementation is not safe yet.

Do not do multiple unrelated fixes in one run.

---

## Decomposition Rules

When decomposing an audit parent:
- Create concrete child issues with actionable scope and appropriate priority/status labels.
- Mark the parent decomposed through Dispatch (`/api/issues/actions/decompose`) with follow-up URLs.
- Do NOT mark the parent Done unless the GitHub issue is actually closed.
- Do NOT leave child implementation work in the escalated lane unless it truly requires escalated/GPT handling.

---

## Final Response Formats

END exactly one of:
- `Escalated lane is clear.`
- `Done. Decomposed {repo} #{number}: {child_urls}.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`

Do not end after local commit only. Push → create/update PR → verify → update Dispatch → STOP.
