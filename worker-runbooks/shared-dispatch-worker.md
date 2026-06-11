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
- `active-follow-up`: act on evidence — fix failing checks, address requested changes, or resolve merge conflict on the returned PR.

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
4. After the step, update Dispatch **both**:
   - `dispatch_work_update.py checkpoint --agent $DISPATCH_AGENT_NAME --checkpoint <STEP_CHECKPOINT> --summary "..."`
   - `dispatch_work_update.py status --agent $DISPATCH_AGENT_NAME --issue-id <id> --repo <repo> --issue-number <num> --status <status>`
5. Stop.

If lane is missing or mismatched, end:
`Stuck: active work lane mismatch or could not be verified.`

## Implementation Gate

## Direct Push Rule (Misospace Only)

All saffron/* and worker-created branches must be pushed **directly** to the
`misospace/*` origin remote. No forks exist for `misospace/*` repos.

- Use the local `/data/git/{repo}` repo. Do not clone fresh.
- `git push origin <branch>` — never via a fork.
- `gh pr create` must always result in a same-org PR. After creating, verify:
  `gh pr view --json isCrossRepository` returns `false`.
- Cross-repo/fork PRs **break the AI PR review CI** because GitHub does not
  forward secrets (e.g. `ACTIONS_APP_ID`, `ACTIONS_APP_PRIVATE_KEY`) to fork
  PR runs. The review job will fail with:
  `Error: The 'client-id' (or deprecated 'app-id') input must be set to a
  non-empty string.`

If `isCrossRepository` is true, the push is wrong. Fix it before reporting
`Done`. Do not just close and recreate the PR — re-push to the right remote
and re-open.

## Implementation Gate

After code changes:

1. validate locally as far as practical
2. commit
3. push
4. open/update PR
5. verify with `gh pr view`
6. Update Dispatch in this exact order:
   a. `dispatch_work_update.py checkpoint --agent $DISPATCH_AGENT_NAME --checkpoint PR_OPENED --summary "Opened PR #N for <repo>#<issue>"`
   b. `dispatch_work_update.py status --agent $DISPATCH_AGENT_NAME --issue-id <id> --repo <repo> --issue-number <num> --status in-review`
7. stop

Never end after local commit only.

## Dispatch Update Rules

- **Checkpoint** (`/api/agent-work/checkpoint`): tracks active-work progress only.
  Valid values: `CLAIMED`, `REPO_PREPARED`, `BRANCH_CREATED`, `CHANGES_MADE`,
  `TESTS_RUNNING`, `PR_OPENED`, `DONE`, `BLOCKED`.
  **Do NOT use checkpoint to set issue status** (e.g. never `--checkpoint in-review`).
- **Status** (`/api/issues/status`): sets issue status label.
  Use `--status in-review` after opening a PR.
  **Do NOT use status to report work progress** (e.g. never `--status PR_OPENED`).
- Use `dispatch_work_update.py` for both — never hand-roll the JSON.

## PR Body

PR body must start with exactly one of:
- `Fixes #{number}`
- `Refs #{number}`

No heading or blank line before that keyword.

## Final Guard

End only with a lane-specific final form from the lane runbook. Validate final
text when practical:

```bash
printf '%s\n' "$FINAL_TEXT" | python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/worker_result_guard.py
```
