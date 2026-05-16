# Mission Control Workflow Scripts

Private repo tracking Mission Control workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and workflow documentation for the Mission Control integration layer.

## Scope

This repo tracks Mission Control workflow files from the Saffron agent workspace:
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
- home-ops or mission-control app code

## Relationship to Mission Control App

The Mission Control application lives separately at `misospace/mission-control`. This repo contains only the agent-side workflow scripts that interact with Mission Control as a consumer.

## Scripts

| Script | Purpose |
|--------|---------|
| `github_followup_watcher.py` | Watch for PR/issue activity by itsmiso-ai |
| `issue_lane_judge.py` | Classify issues into `normal`/`escalated`/`backlog` lanes |
| `pr_fix_queue.py` | PR review-fix queue management |
| `project_backlog_sync.py` | Sync GitHub issues to Vibe Coding project |
| `project_groom.py` | Route issues to Ready/Backlog/lanes |
| `wishlist_read_board.py` | Read Ready normal-lane items from project |
| `wishlist_read_gpt_audit_board.py` | Read Ready escalated-lane audit items |
| `mission_control_reporter.py` | Report agent runs to Mission Control |
| `context-budget.py` | Audit OpenClaw context token overhead |
| `research_before_task.py` | Research GitHub issues before implementing |
| `sync_summary.py` | Sync session summaries to wiki |
| `wishlist-cron-prompt-v2.md` | Wishlist cron prompt template |

## Security

Secrets and credentials must never be committed. All token handling is done via environment variables injected at runtime.