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
- **pr_fix_queue.json** — Ephemeral queue state
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
| `pr_fix_queue.py` | PR review-fix queue management |
| `project_backlog_sync.py` | Sync GitHub issues to Vibe Coding project |
| `project_groom.py` | Route issues to Ready/Backlog/lanes |
| `wishlist_read_board.py` | **DEPRECATED** — Workers now consume Dispatch queue APIs directly (`GET /api/agents/{agentName}/queue?lane=normal`) instead of reading GitHub Project boards. Kept for reference/backwards compatibility. |
| `wishlist_read_gpt_audit_board.py` | **DEPRECATED** — Workers now consume Dispatch queue APIs directly (`GET /api/agents/{agentName}/queue?lane=escalated`) instead of reading GitHub Project boards. Kept for reference/backwards compatibility. |
| `dispatch_reporter.py` | Report agent runs to Dispatch (prefer `DISPATCH_URL`/`DISPATCH_AGENT_TOKEN`; falls back to `MISSION_CONTROL_URL`/`MISSION_CONTROL_AGENT_TOKEN`) |
| `context-budget.py` | Audit OpenClaw context token overhead |
| `research_before_task.py` | Research GitHub issues before implementing |
| `sync_summary.py` | Sync session summaries to wiki |

## Worker Prompt Migration (Issue #70)

Worker cron prompts no longer reference GitHub Project boards. Instead, they consume work from Dispatch queue APIs:

- **Normal lane:** `GET /api/agents/{agentName}/queue?lane=normal`
- **Escalated lane:** `GET /api/agents/{agentName}/queue?lane=escalated`

Workers claim work via `POST /api/issues/claim` and update status via `POST /api/issues/move`. No GitHub Projects GraphQL mutations are used in worker prompts.

Affected cron jobs:
- `(Saffron): 35B Wishlist Chip` — normal lane, uses Dispatch normal queue
- `(Saffron): GPT-5.5 Wishlist Chip` — escalated lane, uses Dispatch escalated queue

## Security

Secrets and credentials must never be committed. All token handling is done via environment variables injected at runtime.