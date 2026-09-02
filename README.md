# Dispatch Workflow Scripts

Workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and cron prompt templates for the
Dispatch integration layer. This repo is the agent-side companion to the
Dispatch application (`misospace/dispatch`).

## Current State

Dispatch sync, grooming, and worker execution are handled by k8s CronJobs
and Foreman. The Saffron heartbeat is a no-op. This repo retains the workflow
prompt and small scripts that active Saffron crons depend on:

| Cron | Script | Schedule |
|------|--------|----------|
| Nightly Tech Debt Audit | `select_audit_repos.py select --count 1` | Daily 1am MT |

## Scripts

| Script | Purpose |
|--------|---------|
| `project_groom.py` | Dispatch queue utilities; `--list-tracked-repos` returns tracked repos from Dispatch API |
| `select_audit_repos.py` | Selects the oldest audit from the live Dispatch repo list and records Saffron-side audit recency |
| `create_audit_issue.py` | Serializes audit issue creation, checks for existing issues, and recovers timed-out creates before retrying |

## Cron Prompts

`cron-prompts/` contains prompt templates for the active audit cron:

| Template | Cron |
|----------|------|
| `weekly-audit.md` | Nightly rotating Misospace audit |

## Excluded

The following must never be committed:
- **HEARTBEAT.md** — PVC-backed runtime copy
- **cron/jobs.json** — Runtime state, managed by `openclaw cron`
- **.state/*** — Runtime queue state
- Any file containing tokens, secrets, or credentials
- Any OpenClaw agent config, session, or memory files

## Security

Secrets and credentials must never be committed. All token handling is done via
environment variables (`DISPATCH_URL`, `DISPATCH_AGENT_TOKEN`) injected at runtime.
