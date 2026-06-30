# Heartbeat Procedure

Run every hour. Dispatch owns the queue. Saffron claims work, does engineering,
reports back.

For the full workflow contract (status lifecycle, lane rules, worker rules,
constraints), see the workspace `AGENTS.md`. This file is the execution
procedure only.

## Contract

Do not answer a heartbeat poll by inspection only. `HEARTBEAT_OK` is valid only
after the deterministic runner has executed.

### 1. Deterministic heartbeat

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/heartbeat.py
```

This calls `POST /api/agents/saffron/heartbeat` which handles:
- best-effort issue sync
- stale status reconciliation
- closed-issue cleanup
- lane consistency
- AgentRun recording

Then probes all three lanes for work availability and toggles worker crons via
`dispatch_worker_cron.py`.

If the runner exits non-zero, surface the failure to Discord:

```
message(action="send", channel="discord", target="channel:1488593762644131940", message="<failure summary>")
```

Then reply `HEARTBEAT_FAILED` with the error output.

### 2. Grooming work

After the deterministic heartbeat, check for Dispatch-assigned grooming work:

```bash
curl -fsS -H "Authorization: Bearer $DISPATCH_AGENT_TOKEN" \
  "$DISPATCH_URL/api/agents/saffron/next-task?mode=groom"
```

If the response has `type: "groom"`, trigger the hosted groomer to handle the
intelligence work (LLM call, validation, label/lane mutations):

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $DISPATCH_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"issueNumber": <number>, "repoFullName": "<owner/repo>", "dryRun": false}' \
  "$DISPATCH_URL/api/groomer/run"
```

The hosted groomer does the LLM call, output validation, and mutation. Saffron's
role is to trigger it and report the outcome.

If the groomer returns `status: "failed"`, report the error. If
`status: "dry_run_completed"`, report that this was a dry run only.
The `output` field contains labels, lane, summary, and actionability details.

If `type: "idle"`, no grooming work exists. Continue.

### 3. No work

If no deterministic failures occurred, no grooming work exists, and no
user-relevant state changes happened, reply `HEARTBEAT_OK`.

## Surface Only

Only send a user-visible heartbeat reply for:

- errors
- actionable follow-up
- state changes
- user-relevant results
- grooming outcomes that need human attention (`needs-human` / `needs-info`)
- remaining backlog pressure

Do not surface routine sync/reconcile/probe results.

## Failure Reporting

If `heartbeat.py` exits non-zero, the heartbeat **must** surface the failure to
Discord via the message tool:

```
message(action="send", channel="discord", target="channel:1488593762644131940", message="<failure summary>")
```

## Worker Cron Control

The heartbeat script handles cron enable/disable automatically based on lane
probe results. The dedicated actuator `scripts/dispatch_worker_cron.py` is the
only thing allowed to mutate worker cron state.

- **enabled/disabled only** — schedule, model, prompt, delivery, and alerts are operator-owned
- **only via `dispatch_worker_cron.py` with `--apply`**
- The actuator refuses any other cron setting

If a probe returns `stuck` or `needsAttention`, the cron is left unchanged and
the heartbeat surfaces the issue.

## Rules

- GitHub issues/PRs are authoritative for issue state, labels, PRs, and closure.
- Dispatch owns work discovery, claims, lane assignment, queue ordering, worker
  enablement, active work, stale leases, checkpoints, and `nextAction`.
- GitHub Projects are deprecated. Heartbeat/workers must not read or mutate them.
- `Done` means GitHub issue closed/terminal. Open PRs mean `status/in-review`.
- Stale open-issue `status/done` cleanup goes through Dispatch status APIs.
- Renovate issues are excluded unless explicitly requested.

## Weekly Audit

`(Saffron): Weekly Audit of Misospace` runs Wednesdays at 1am MT on MiniMax-M2.7.
Frontier-lane work runs on GLM-5.2. GPT-5.5 is available as a manual escalation
path if GLM-5.2 underperforms.

## Legacy Scripts (Deprecated)

The following scripts are DEPRECATED and fenced behind `--legacy-dangerous*`
flags. Normal heartbeat MUST NOT invoke them:

- `project_backlog_sync.py` — Dispatch heartbeat handles sync
- `project_groom.py` — Dispatch owns grooming/lane/status lifecycle
- `backlog_groomer.py` — Dispatch `next-task?mode=groom` surfaces candidates
- `issue_lane_judge.py` — Dispatch owns lane assignment
- `sync_summary.py` — Wrapper around deprecated sync
- `wishlist_read_board.py` — Use `GET /api/agents/{agentName}/queue`
- `wishlist_read_gpt_audit_board.py` — Use `GET /api/agents/{agentName}/queue`

`audit_decompose.py` remains for the nightly audit decomposer cron — it creates
child issues from audit umbrella parents. It is not part of the normal heartbeat.
