# Cron Prompt Templates

This directory stores sanitized prompt templates for Dispatch workflow cron
jobs. It is safe for a public repository by design:

- no `cron/jobs.json`
- no schedules
- no delivery targets or channel IDs
- no runtime state
- no tokens, secrets, or concrete env values
- no private one-off personal prompts

Use placeholders for runtime-specific values:

- `{{WORKFLOW_DIR}}`
- `{{DISPATCH_LOCAL_AGENT}}`
- `{{DISPATCH_CLOUD_AGENT}}`
- `{{DISPATCH_FRONTIER_AGENT}}`
- `{{BLOCKED_MERGE_REPOS}}`
- `{{TRACKED_PR_AUTHORS}}`

## Templates

| Template | Lane |
|----------|------|
| `local-worker.md` | Local lane worker (nvidia) |
| `cloud-worker.md` | Cloud lane worker (dsv4p) |
| `frontier-worker.md` | Frontier lane worker (glm-5.2) |
| `daily-pr-review.md` | Daily PR review |
| `weekly-audit.md` | Weekly Misospace audit |
| `nightly-audit-decomposer.md` | Nightly audit decomposer |

Live cron jobs are still managed through `openclaw cron`. These templates are
for review, versioning, and drift control.
