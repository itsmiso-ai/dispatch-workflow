# Shared Dispatch Worker Policy

Rules for Saffron worker sessions in any lane. Read the lane-specific runbook
after this file.

## Source Of Truth

- Use `DISPATCH_URL`, `DISPATCH_AGENT_TOKEN`, and `DISPATCH_AGENT_NAME`.
- Do not use `MISSION_CONTROL_*`.
- GitHub issues/PRs are authoritative for issue state, labels, PRs, and closure.
- Dispatch owns queues, claims, leases, checkpoints, lane assignment, status
  transitions, and `nextAction`.
- GitHub Projects are deprecated. Do not read or mutate them.

## Status Contract

| Status | Meaning |
|---|---|
| `status/backlog` | needs triage/grooming; not claimable |
| `status/ready` | groomed/actionable; claimable |
| `status/in-progress` | claimed or implementation started |
| `status/in-review` | PR opened; issue still open |
| `status/done` | GitHub issue closed/terminal only |

Opening or updating a PR is not Done. Open issue plus open PR means
`status/in-review`.

## Deterministic Preflight

Run the lane-specific `dispatch_worker_preflight.py` command before model
judgment. It owns PR-fix queue lookup, active-work lookup, lane verification,
queue selection, repo workspace checks, and optional claim.

Preflight actions:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: update the returned PR only.
- `resume-active-work`: obey returned `nextAction` exactly.
- `claim-ready-issue`: implement returned claimed Ready issue.

If preflight says `stuck`, do not guess past it.

## Work Rules

- PR-fix queue takes precedence over new issue work.
- One item per run. One bounded step per run.
- Do not consume `status/backlog`.
- Do not consume Renovate issues unless explicitly requested.
- Ignore work claimed by another agent.
- Before coding, check for an existing PR. Do not open duplicates.
- Workers update existing PR branches for PR-fix work.

## Active Work

If Dispatch returns active work:

1. Verify the lane matches the worker lane.
2. Obey `nextAction` exactly.
3. Do one bounded step.
4. Update Dispatch checkpoint/finish/status.
5. Stop.

If lane is missing or mismatched, end:
`Stuck: active work lane mismatch or could not be verified.`

## Implementation Gate

After code changes:

1. validate locally as far as practical
2. commit
3. push
4. open/update PR
5. verify with `gh pr view`
6. update Dispatch to `status/in-review` or checkpoint/finish
7. stop

Never end after local commit only.

## PR Body

PR body must start with exactly one of:
- `Fixes #{number}`
- `Refs #{number}`

No heading or blank line before that keyword.

## Final Guard

End only with a lane-specific final form from the lane runbook. Validate final
text when practical:

```bash
printf '%s\n' "$FINAL_TEXT" | python3 /home/node/.openclaw/workspace-saffron/scripts/worker_result_guard.py
```
