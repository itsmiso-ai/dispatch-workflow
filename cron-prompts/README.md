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
- `{{DISPATCH_NORMAL_AGENT}}`
- `{{DISPATCH_ESCALATED_AGENT}}`
- `{{BLOCKED_MERGE_REPOS}}`
- `{{TRACKED_PR_AUTHORS}}`

Live cron jobs are still managed through `openclaw cron`. These templates are
for review, versioning, and drift control.
