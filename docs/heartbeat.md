# HEARTBEAT.md

Run every hour.

## Contract

Do not answer a heartbeat poll by inspection only. `HEARTBEAT_OK` is valid only
after the deterministic runner has executed and any due Saffron-owned grooming
has been handled or explicitly failed.

First run deterministic plumbing:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/heartbeat.py
```

If the runner exits non-zero, surface the error output and stop.

Then collect backlog candidates:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/backlog_groomer.py --max 10
```

If there are no candidates and no user-relevant deterministic results, reply
`HEARTBEAT_OK`.

If candidates exist, Saffron owns the intelligence work. Groom them directly in
this heartbeat turn or spawn a Saffron sub-agent with the candidate request JSON.
Do not call a model from Python, curl, raw HTTP, or any script-owned model path.
If sub-agent/tooling is unavailable and candidates exist, surface
`GROOMING_FAILED` with the reason.

## Runner Scope

`dispatch-workflow/scripts/heartbeat.py` owns only deterministic heartbeat
plumbing: Dispatch PR follow-up sync, best-effort Dispatch scheduled sync,
deterministic Dispatch reconciliation/lane cleanup, cron enablement, and
best-effort Dispatch run reporting.

`dispatch-workflow/scripts/backlog_groomer.py` is deterministic only. It
collects open `status/backlog` candidates and writes a JSON request under
`dispatch-workflow/.state/backlog_grooming_requests/`. It does not perform
judgment and must not call a model.

Backlog judgment is Saffron agent work. For each candidate, inspect the issue and
upstream repo in `/data/git/*` when useful, then apply the decision through
Dispatch:

- ready -> `POST /api/issues/groom` with `action: "promote_to_ready"`
- escalated -> `POST /api/issues/groom` with `action: "escalate"`
- needs-info -> `POST /api/issues/groom` with `action: "mark_needs_info"`
- needs-human / not ready -> `POST /api/issues/groom` with
  `action: "mark_not_ready"`
- blocked -> `POST /api/issues/groom` with `action: "mark_blocked"`

Labels are the source of truth. `status/backlog` is not ready; workers only get
work after Saffron/Dispatch moves it to `status/ready`.

## Surface Only

Only send a user-visible heartbeat reply for:

- errors
- actionable follow-up
- state changes
- user-relevant results
- grooming human-attention outcomes: `needs-human` / `needs-info`
- `GROOMING_FAILED` or remaining backlog pressure after the grooming cap

Do not surface routine `ready`, `escalated`, `decompose`, or `keep-backlog`
grooming outcomes unless there is an error.

## Rules

- GitHub issues/PRs are authoritative for issue state, labels, PRs, and closure.
- Dispatch owns work discovery, claims, lane assignment, queue ordering, worker
  enablement, active work, stale leases, checkpoints, and `nextAction`.
- GitHub Projects are deprecated. Heartbeat/workers must not read or mutate them.
- `Done` means GitHub issue closed/terminal. Open PRs mean `status/in-review`.
- Stale open-issue `status/done` cleanup must go through Dispatch status APIs.
- Renovate issues are excluded unless explicitly requested.

## Worker Crons

- `(Saffron): MC: Normal` consumes normal lane work.
- `(Saffron): MC: Escalated` consumes escalated lane work.
- Both check PR review-fix queue first, then Dispatch queue work.
- Both consume exactly one actionable item, obey `nextAction`, report, and stop.

## Weekly Audit

`(Saffron): Weekly Audit of Misospace by GPT-5.5` runs Wednesdays at 1am MT. GPT
model use is reserved for this weekly audit and escalated-lane work only.
