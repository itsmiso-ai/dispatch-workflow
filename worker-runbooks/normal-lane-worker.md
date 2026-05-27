# Normal-Lane Worker — Dispatch Queue Consumption

Lane: `normal`. This worker is identified as `$DISPATCH_AGENT_NAME=saffron-normal`.

---

## Active-Work Lane Verification

Before obeying Dispatch active-work `nextAction` / `checkpoint`:

1. Inspect the active work's lane field.
2. If the lane is `normal` → proceed with step 2 of the active-work resumption flow.
3. If the lane is `escalated` or cannot be verified → stop. END: `Stuck: active work lane mismatch or could not be verified.`

Do not silently discard mismatched active work. Do not mutate Dispatch state for mismatched active work.

---

## PR-Fix Queue (Precedence)

Check before picking new work:

```bash
DISPATCH_AGENT_NAME=saffron-normal python3 /home/node/.openclaw/workspace-saffron/scripts/pr_fix_queue.py next --lane normal
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

## Select Ready Work

```bash
curl -fsS "$DISPATCH_URL/api/agents/$DISPATCH_AGENT_NAME/queue?lane=normal"
```

Select exactly one actionable item:
- Prefer the first item already claimed by `agent/$DISPATCH_AGENT_NAME` if Dispatch returns it.
- Otherwise choose the first unclaimed item with `status/ready` (or no status) if Dispatch marks it claimable.
- Do NOT choose `status/backlog`; Backlog is triage only.
- Ignore items claimed by other agents (`agent/<anything other than $DISPATCH_AGENT_NAME>`).
- Do NOT choose Renovate issues unless the item explicitly says Renovate work is requested.

If no $DISPATCH_AGENT_NAME-claimed or unclaimed claimable Ready work exists: END: `Pipeline is clear.`

---

## Implementation

1. **Bounded step:** In `/data/git/{repo-name}`: `git fetch origin && git checkout main && git pull origin main`, create/reuse a branch, inspect the issue, implement the smallest focused fix, run targeted validation, commit.
2. **PR:** Open/update PR, verify with `gh pr view`.
3. **Dispatch:** Set status to In Review through Dispatch status/checkpoint APIs.
4. STOP after one bounded unit.

---

## Final Response Formats

END exactly one of:
- `Pipeline is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`

Do not end after local commit only. Push → create/update PR → verify → update Dispatch → STOP.
