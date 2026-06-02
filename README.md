# Dispatch Workflow Scripts

Dispatch workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and workflow documentation for the Dispatch integration layer.

This repository is intended to be safe to make public: it should contain only
workflow code, runbooks, and docs. Runtime state, credentials, OpenClaw
configuration, memory, cron state, and local workspace artifacts are excluded.

## Scope

This repo tracks Dispatch workflow files from the Saffron agent workspace:
- Python scripts for heartbeat grooming, lane judging, backlog syncing
- Bash/shell utility scripts
- Workflow documentation and runbooks

## Excluded

The following are intentionally excluded and must never be committed:
- **HEARTBEAT.md** — PVC-backed, not versioned
- **cron/jobs.json** — Runtime state, managed by `openclaw cron`
- **.state/*** — Runtime queue state and watch lists
- **pr_fix_queue.json** — Legacy local queue state; active PR-fix state lives in Dispatch
- **github_followup_watcher.json** — Runtime watch state
- Any file containing tokens, secrets, or credentials
- Any OpenClaw agent config, session, or memory files
- home-ops or dispatch app code

## Relationship to Dispatch App

The Dispatch application lives separately at `misospace/dispatch`. This repo contains only the agent-side workflow scripts that interact with Dispatch as a consumer. 

## Scripts

| Script | Purpose |
|--------|---------|
| `github_followup_watcher.py` | Watch for PR/issue activity by itsmiso-ai |
| `issue_lane_judge.py` | Classify issues into `normal`/`escalated`/`backlog` lanes |
| `pr_fix_queue.py` | Compatibility CLI for Dispatch-backed PR review-fix queue management |
| `dispatch_worker_preflight.py` | Deterministic Normal/Escalated worker preflight: PR-fix, active work, lane verification, queue selection, optional claim |
| `worker_result_guard.py` | Validate Normal/Escalated worker final text against the terminal worker contract |
| `heartbeat.py` | Run the compact heartbeat contract: watcher, sync, grooming, bounded enrichment, and Dispatch run reporting |
| `backlog_groomer.py` | Bounded self-hosted backlog grooming wrapper used by heartbeat |
| `project_backlog_sync.py` | Compatibility wrapper for Dispatch scheduled sync (`POST /api/sync/scheduled`); no GitHub Projects access |
| `project_groom.py` | Dispatch v0.3 grooming: scheduled sync, status reconciliation, lane classification, cron enablement |
| `wishlist_read_board.py` | Compatibility reader for Dispatch normal queue; does not query GitHub Projects |
| `wishlist_read_gpt_audit_board.py` | Compatibility reader for Dispatch escalated queue; does not query GitHub Projects |
| `dispatch_reporter.py` | Report agent runs to Dispatch using only `DISPATCH_URL`/`DISPATCH_AGENT_TOKEN` |
| `dispatch_work_update.py` | Update Dispatch checkpoints and issue status from worker sessions |
| `research_before_task.py` | Research GitHub issues before implementing |
| `sync_summary.py` | Compact Dispatch sync summary helper |

## Dispatch v0.3 Worker Semantics

Worker cron prompts no longer reference GitHub Project boards. Instead, they consume work from Dispatch queue APIs and Dispatch-owned lifecycle state:

- **Normal lane:** `GET /api/agents/{agentName}/queue?lane=normal`
- **Escalated lane:** `GET /api/agents/{agentName}/queue?lane=escalated`

Workers claim work via `POST /api/issues/claim` and update lifecycle status via Dispatch status/lease/checkpoint APIs. GitHub Projects are fully deprecated and must not be queried or mutated by active workflow scripts.

Normal/Escalated worker selection starts with deterministic local preflight, not model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-normal python3 scripts/dispatch_worker_preflight.py --lane normal --claim --json
DISPATCH_AGENT_NAME=saffron-escalated python3 scripts/dispatch_worker_preflight.py --lane escalated --claim --json
```

The preflight result action decides the worker path:
- `clear` / `stuck` — reply with the provided `terminal` and stop.
- `pr-fix` — update the existing PR only.
- `resume-active-work` — obey the returned `nextAction` exactly.
- `claim-ready-issue` — implement the returned claimed Ready issue.

Cron result text must match the terminal contract. Use:

```bash
printf '%s\n' "$FINAL_TEXT" | python3 scripts/worker_result_guard.py
```

Board status contract:
- `status/backlog` = needs triage/grooming, not ready for agents.
- `status/ready` = groomed/actionable and available to claim.
- `status/in-progress` = claimed or implementation started.
- `status/in-review` = PR opened/checks/review pending while the issue remains open.
- `status/done` = GitHub issue is closed/terminal only.

Hard rule: opening or updating a PR is not Done. An open issue with an unmerged PR must be In Review, not Done.

Work selection:
- PR-fix queue items from Dispatch have precedence.
- Workers consume exactly one actionable item per run.
- Workers prefer Ready work and do not consume Backlog unless explicitly requested.
- Renovate issues are excluded from agent queues unless explicitly requested.
- If Dispatch returns active work, a checkpoint, or `nextAction`, workers obey that next action exactly, perform one bounded step, update Dispatch with the result/checkpoint, and stop.

## Bounded LLM-assisted backlog grooming

`project_groom.py` has an explicit backlog investigation mode for open issues that are stuck in `status/backlog`. Heartbeat runs deterministic sync/grooming first, then uses this bounded intelligence step to enrich up to 3 previously ungroomed backlog issues per run.

Dry-run investigation, no mutations:

```bash
python3 scripts/project_groom.py --no-sync --groom-backlog --groom-backlog-use-llm --groom-backlog-only --groom-backlog-max 5
```

Apply recommendations after reviewing the report:

```bash
python3 scripts/project_groom.py --no-sync --groom-backlog --groom-backlog-use-llm --groom-backlog-only --groom-backlog-apply --groom-backlog-max 5
```

The grooming pass requires the explicit `--groom-backlog-use-llm` flag before it will call a model. It uses `BACKLOG_GROOMING_MODEL` (default `litellm/self-hosted`) to read issue metadata and recent comments, then records a JSONL report under `.state/backlog_grooming_reports/`. The script refuses GPT models for backlog grooming; use MiniMax/self-hosted here, and reserve GPT for the weekly audit and escalated-lane cron only.

Recommendations are one of:
- `ready` — promote to `status/ready` and keep/use the recommended lane.
- `escalated` — promote to `status/ready` on the escalated lane.
- `needs-info` or `needs-human` — keep out of Ready and surface as a human-attention escalation.
- `decompose` or `keep-backlog` — keep out of Ready and record the reason/next action in the report without surfacing as an escalation.

With `--groom-backlog-apply`, the script uses Dispatch APIs for status/lane updates and may post a guarded GitHub enrichment comment unless `--groom-backlog-no-comment` is set. Comments are only posted when they add missing detail or surface a non-ready reason; fully specified ready issues are promoted without a redundant grooming note. The groomer also re-checks live GitHub state before investigation and apply, so closed or already-ready issues are skipped even if Dispatch cache is stale.

Affected cron jobs:
- `(Saffron): MC: Normal` — normal lane, uses Dispatch normal queue
- `(Saffron): MC: Escalated` — escalated lane, uses Dispatch escalated queue

## Security

Secrets and credentials must never be committed. All token handling is done via environment variables injected at runtime.
