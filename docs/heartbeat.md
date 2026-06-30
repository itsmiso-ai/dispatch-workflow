# HEARTBEAT.md

Run every hour. GROOM SOME FUCKING ISSUES GODDAMNIT.

## Contract

Do not answer a heartbeat poll by inspection only. `HEARTBEAT_OK` is valid only
after the deterministic runner has executed and any due Saffron-owned grooming
has been handled or explicitly failed.

First run deterministic plumbing:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/heartbeat.py
```

If the runner exits non-zero, surface the failure to Discord **using the message
tool** to channel `channel:1488593762644131940` (the Saffron automation board
channel), then reply `HEARTBEAT_FAILED` with the error output. Do not silently
fail — the message tool is the fallback for cron delivery problems.

Then collect backlog candidates:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/backlog_groomer.py --max 10 --include-no-status
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
deterministic Dispatch reconciliation/lane cleanup, worker queue visibility
(via `dispatch_work_probe.py`), and best-effort Dispatch run reporting. The
deterministic plumbing never mutates cron state.

`dispatch-workflow/scripts/dispatch_work_probe.py` is read-only. It answers
"would this lane/agent do work if the worker ran?" by wrapping
`dispatch_worker_preflight.build_packet(claim=False)`. It is the single source
of truth for heartbeat/grooming work detection.

`dispatch-workflow/scripts/dispatch_worker_cron.py` is the only actuator
allowed to mutate worker cron enabled state. It only runs the whitelisted
`openclaw cron edit <id> --enable|--disable` command and refuses to touch
schedule, model, prompt, delivery, alerts, or any other cron setting. It
defaults to dry-run; pass `--apply` to actually mutate.

`dispatch-workflow/scripts/backlog_groomer.py` is deterministic only. It
collects open `status/backlog` candidates plus unlabeled/no-status issues and writes a JSON request under
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

## Failure Reporting

If `dispatch-workflow/scripts/heartbeat.py` exits non-zero or the backlog
groomer fails critically, the heartbeat **must** surface the failure to Discord
via the message tool:

```
message(action="send", channel="discord", target="channel:1488593762644131940", message="<failure summary>")
```

The message tool is the authoritative failure path because cron `failureAlert`
is configured silently and may not reach LilDrunkenSmurf directly. Surface every
heartbeat failure to the Saffron automation board channel so it's visible even
when DM delivery is suppressed.

## Rules

- GitHub issues/PRs are authoritative for issue state, labels, PRs, and closure.
- Dispatch owns work discovery, claims, lane assignment, queue ordering, worker
  enablement, active work, stale leases, checkpoints, and `nextAction`.
- GitHub Projects are deprecated. Heartbeat/workers must not read or mutate them.
- `Done` means GitHub issue closed/terminal. Open PRs mean `status/in-review`.
- Stale open-issue `status/done` cleanup must go through Dispatch status APIs.
- Renovate issues are excluded unless explicitly requested.

## Worker Crons (explicit policy + dedicated actuator)

- `(Saffron): MC: Normal` consumes normal lane work.
- `(Saffron): MC: Escalated` consumes escalated lane work.
- Both check PR review-fix queue first, then Dispatch queue work.
- Both consume exactly one actionable item, obey `nextAction`, report, and stop.

### The little cron goblin gets a badge and a desk

Cron state is no longer mutated as a hidden side effect of grooming. The
heartbeat (Saffron agent) owns the **policy decision**. The dedicated
actuator `scripts/dispatch_worker_cron.py` is the only thing allowed to
mutate the enabled/disabled flag. Schedule, model, prompt, delivery, and
alerts stay operator-owned and out of scope for the actuator.

The boundary is now:

- **Heartbeat (Saffron):** policy. Decides whether the worker should run.
- **`scripts/dispatch_work_probe.py`:** read-only. Answers "would this
  lane/agent do work if the worker ran?".
- **`scripts/dispatch_worker_cron.py`:** deterministic actuator. Enable or
  disable only. Refuses any other cron setting.
- **`scripts/project_groom.py`:** grooming/reporting only. Never mutates
  cron state.

### The exact flow (do this, in this order)

After `python3 scripts/heartbeat.py` returns, the heartbeat **must** run
these commands before doing anything else. Read each probe's JSON, then call
the actuator with the matching `--enable` or `--disable` flag and the
probe verdict in the reason.

```bash
# 1. Probe both lanes. The probe is the source of truth for work.
python3 scripts/dispatch_work_probe.py --lane normal --json
python3 scripts/dispatch_work_probe.py --lane escalated --json
```

```bash
# 2. If probe.hasWork == true, enable the cron for that lane:
python3 scripts/dispatch_worker_cron.py --lane normal \
  --enable --reason "<probe verdict, e.g. active-follow-up>" --apply --json
python3 scripts/dispatch_worker_cron.py --lane escalated \
  --enable --reason "<probe verdict, e.g. ready-issue>" --apply --json
```

```bash
# 3. If probe.hasWork == false, disable the cron for that lane:
python3 scripts/dispatch_worker_cron.py --lane normal \
  --disable --reason "<probe verdict, e.g. clear>" --apply --json
python3 scripts/dispatch_worker_cron.py --lane escalated \
  --disable --reason "<probe verdict, e.g. clear>" --apply --json
```

```bash
# 4. If probe.action == "stuck", do NOT silently enable or disable.
#    Surface `needsAttention` in the heartbeat reply. Leave cron state
#    unchanged and let the next heartbeat (or a human) decide.
```

No other `openclaw cron edit` invocation is allowed. The actuator refuses
schedule/model/prompt/delivery/alerts flags and only emits
`openclaw cron edit <id> --enable|--disable`.

### Allowed cron mutation

- enabled/disabled only
- only via `scripts/dispatch_worker_cron.py`
- only with `--apply`

### Forbidden cron mutation (the actuator will not run these)

- schedule
- model
- prompt
- delivery
- alerts
- any unrelated cron config

### Forbidden pattern

`project_groom.py` must never call `openclaw cron edit` or any cron
mutation. It is grooming/reporting only.

## Weekly Audit

`(Saffron): Weekly Audit of Misospace by GPT-5.5` runs Wednesdays at 1am MT. GPT
model use is reserved for this weekly audit and escalated-lane work only.
