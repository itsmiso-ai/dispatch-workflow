# Dispatch Workflow Scripts

Workflow scripts for the Saffron agent workspace.

**Owner:** Saffron (OpenClaw agent) — `itsmiso-ai` account

**Purpose:** Version-controlled scripts and cron prompt templates for the
Dispatch integration layer. This repo is the agent-side companion to the
Dispatch application (`misospace/dispatch`).

## Current State

Dispatch sync, grooming, and worker execution are handled by k8s CronJobs
and Foreman. The Saffron heartbeat is a no-op. This repo retains only the
two scripts that active Saffron crons depend on:

| Cron | Script | Schedule |
|------|--------|----------|
| Weekly Audit of Misospace | `project_groom.py --list-tracked-repos` | Wed 1am MT |
| Nightly Audit Decomposer | `audit_decompose.py --scan --apply` | Daily 2am MT |

## Scripts

| Script | Purpose |
|--------|---------|
| `audit_decompose.py` | Nightly audit decomposer: creates child issues from audit umbrella parents |
| `project_groom.py` | Dispatch queue utilities; `--list-tracked-repos` returns tracked repos from Dispatch API |

## Cron Prompts

`cron-prompts/` contains prompt templates for the two active crons:

| Template | Cron |
|----------|------|
| `weekly-audit.md` | Weekly Misospace audit |
| `nightly-audit-decomposer.md` | Nightly audit decomposition |

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
