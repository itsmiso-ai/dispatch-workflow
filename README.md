# Dispatch Workflow Scripts

Private repo tracking Dispatch workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and workflow documentation for the Dispatch integration layer.

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
| `project_backlog_sync.py` | Compatibility wrapper for Dispatch scheduled sync (`POST /api/sync/scheduled`); no GitHub Projects access |
| `project_groom.py` | Dispatch v0.3 grooming: scheduled sync, status reconciliation, lane classification, cron enablement |
| `wishlist_read_board.py` | Compatibility reader for Dispatch normal queue; does not query GitHub Projects |
| `wishlist_read_gpt_audit_board.py` | Compatibility reader for Dispatch escalated queue; does not query GitHub Projects |
| `dispatch_reporter.py` | Report agent runs to Dispatch using only `DISPATCH_URL`/`DISPATCH_AGENT_TOKEN` |
| `context-budget.py` | Audit OpenClaw context token overhead |
| `research_before_task.py` | Research GitHub issues before implementing |
| `sync_summary.py` | Sync session summaries to wiki |

## Dispatch v0.3 Worker Semantics

Worker cron prompts no longer reference GitHub Project boards. Instead, they consume work from Dispatch queue APIs and Dispatch-owned lifecycle state:

- **Normal lane:** `GET /api/agents/{agentName}/queue?lane=normal`
- **Escalated lane:** `GET /api/agents/{agentName}/queue?lane=escalated`

Workers claim work via `POST /api/issues/claim` and update lifecycle status via Dispatch status/lease/checkpoint APIs. GitHub Projects are fully deprecated and must not be queried or mutated by active workflow scripts.

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

## LLM-assisted backlog grooming

`project_groom.py` has an explicit backlog investigation mode for issues that are stuck in `status/backlog`.

Dry-run investigation, no mutations:

```bash
python3 scripts/project_groom.py --no-sync --groom-backlog --groom-backlog-only --groom-backlog-max 5
```

Apply recommendations after reviewing the report:

```bash
python3 scripts/project_groom.py --no-sync --groom-backlog --groom-backlog-only --groom-backlog-apply --groom-backlog-max 5
```

The grooming pass uses an LLM (`BACKLOG_GROOMING_MODEL`, default `openai-codex/gpt-5.5`) to read issue metadata and recent comments, then records a JSONL report under `.state/backlog_grooming_reports/`.

Recommendations are one of:
- `ready` — promote to `status/ready` and keep/use the recommended lane.
- `escalated` — promote to `status/ready` on the escalated lane.
- `decompose`, `needs-info`, `needs-human`, or `keep-backlog` — keep out of Ready and surface the reason/next action in the report.

With `--groom-backlog-apply`, the script uses Dispatch APIs for status/lane updates and posts a guarded GitHub enrichment comment unless `--groom-backlog-no-comment` is set.

Affected cron jobs:
- `(Saffron): 35B Wishlist Chip` — normal lane, uses Dispatch normal queue
- `(Saffron): GPT-5.5 Wishlist Chip` — escalated lane, uses Dispatch escalated queue

## Security

Secrets and credentials must never be committed. All token handling is done via environment variables injected at runtime.
