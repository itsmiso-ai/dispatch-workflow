# HEARTBEAT.md

Run every hour.

Only surface a reply when there is an error, actionable follow-up, state change, or user-relevant result. If nothing needs attention, reply `HEARTBEAT_OK`.

## 1. GitHub Follow-Up Watcher

Scope: all reachable GitHub repos.

Script:
- /home/node/.openclaw/workspace-saffron/scripts/github_followup_watcher.py

Run:
- python3 /home/node/.openclaw/workspace-saffron/scripts/github_followup_watcher.py

Watch:
- Open issues/PRs authored by itsmiso-ai
- New non-self comments
- New maintainer comments
- New PR review comments / requested changes
- State changes on watched items

State:
- Global watcher, not limited to backlog repos.
- Persists local state and reports only newly-seen comments, reviews, and PR state changes.
- Queues actionable PR review fixes in `/home/node/.openclaw/workspace-saffron/.state/pr_fix_queue.json` for cron consumption.

Queue low-risk PR fix requests:
- Missing file / small requested tweak
- Typos, docs, wording fixes
- Simple path/copy/rebase-style corrections
- Straightforward "please add X" with no scope change
- Requested changes / actionable automated review findings / newly failing checks

PR review-fix queue rules:
- Normal lane PR fixes are consumed by `(Saffron): MC: Noelle` before new board items.
- Escalated lane PR fixes are consumed by `(Saffron): MC: Varka` before new board items.
- Workers update the existing PR branch only; they must not open duplicate PRs.
- Ambiguous, architecture/security/policy/scope feedback is blocked as `needs-human`.

Escalate to LilDrunkenSmurf:
- Scope or requirement changes
- Architecture/design changes
- Security concerns
- Process/policy disagreements
- Closing/replacing PRs
- Anything ambiguous or high-blast-radius

Full procedure: github-core skill.

## 2. Vibe Coding Backlog Grooming

Project:
- Name: Vibe Coding Backlog
- ID: PVT_kwHOAsG-YM4BTyY3
- URL: https://github.com/users/joryirving/projects/1
- Columns: Triage → Backlog → Ready → In Progress → Done

Tracked repos:
- misospace/miso-chat
- misospace/miso-gallery
- misospace/dispatch
- misospace/pr-reviewer-action
- misospace/windowstead

### Step 1: Sync Backlog

Script:
- /home/node/.openclaw/workspace-saffron/scripts/project_backlog_sync.py

Run:
- python3 /home/node/.openclaw/workspace-saffron/scripts/project_backlog_sync.py

Rules:
- Add qualifying issues to Triage.
- Qualifying labels: enhancement, bug, priority/*, audit, needs-gpt, needs-escalation
- Weekly audit issues must be picked up so groom can route large findings to the Escalated lane.
- Skip labels: dependencies, skip-changelog, internal
- Skip items already in the project.

### Step 2: Groom Triage

Script:
- /home/node/.openclaw/workspace-saffron/scripts/project_groom.py

Run:
- python3 /home/node/.openclaw/workspace-saffron/scripts/project_groom.py

Rules:
- bug OR priority/p0 OR priority/p1 → Ready
- enhancement without bug/priority → Backlog
- Heartbeat/groom owns lane assignment. Crons consume lanes; they do not decide Escalated eligibility.
- `project_groom.py` uses the model-backed `issue_lane_judge.py` for concrete issue routing:
  - `normal` → Ready without `needs-escalation`/`needs-gpt`; worked by wishlist cron.
  - `escalated` → Ready with `needs-escalation`/`needs-gpt`; worked by Escalated audit cron.
  - `backlog` → Backlog; not actionable yet.
- Audit parent / umbrella issues:
  - placeholder/no findings yet → Backlog
  - broad/systemic findings or decomposition/design needed → Ready for Escalated audit chipping
  - already decomposed → Backlog
- Normal wishlist cron handles queued normal PR fixes first, then Ready/In Progress work excluding audit/umbrella/`needs-gpt`.
- Escalated audit chipping cron handles queued Escalated PR fixes first, then audit/umbrella/`needs-gpt` Ready/In Progress work after heartbeat assignment.
- `project_groom.py` keeps lane crons enabled when queued PR fixes exist, even if the project Ready/In Progress lane is otherwise empty.

## 3. Report to Dispatch

After all above steps complete, report the heartbeat run to Dispatch.

Script:
- /home/node/.openclaw/workspace-saffron/scripts/mission_control_reporter.py

Collect run metadata:
- `startedAt`: ISO8601 UTC timestamp captured before Step 1 (or use current UTC time)
- `finishedAt`: ISO8601 UTC timestamp captured after all steps above
- `status`: "ok" if all steps completed normally, "warning" if non-fatal issues occurred, "error" if a step failed
- `summary`: brief one-line description of what the heartbeat did (e.g. "Checked assigned issues, reviewed stale PRs, no action required.")
- `touchedIssueUrls`: collect all GitHub issue/PR URLs from heartbeat output lines (github_followup_watcher events, project_groom move/close actions)

Run:
```
python3 /home/node/.openclaw/workspace-saffron/scripts/mission_control_reporter.py \
    --started-at "<startedAt>" \
    --finished-at "<finishedAt>" \
    --status <ok|warning|error> \
    --summary "<summary>" \
    --touched <url1> <url2> ...
```

Reporting is best-effort:
- If Dispatch is unreachable or env vars are missing, log a warning and continue — do not fail the heartbeat.
- Never print `DISPATCH_AGENT_TOKEN` (or legacy `MISSION_CONTROL_AGENT_TOKEN`).
- Do not include the token in logs even in partial form.

Required environment variables (must be configured externally):
- `DISPATCH_URL` — base URL for Dispatch (e.g. `http://dispatch.llm.svc.cluster.local:3000`); falls back to `MISSION_CONTROL_URL`
- `DISPATCH_AGENT_TOKEN` — bearer token for Dispatch; falls back to `MISSION_CONTROL_AGENT_TOKEN`

## Response Contract

Report, in order, only when surfacing a heartbeat reply is warranted:
1. Follow-up watcher results.
2. Backlog sync stats: qualified, added, skipped.
3. Groom results: moved/promoted/status.
4. PR review-fix queue counts/blockers.
5. Any errors.

## Wishlist Crons

`(Saffron): MC: Noelle` runs every 2h with a 1h timeout. It checks the normal PR review-fix queue first, then reads the project Ready/In Progress normal lane via GraphQL API and works items in priority order. Its prompt has a hard completion gate: after any commit it must push, create/update/verify the PR with `gh pr view`, and final with a PR URL or `Stuck: {reason}`.

`(Saffron): MC: Varka` runs every 2h when enabled with a 2h timeout. It checks the Escalated PR review-fix queue first, then reads the audit/`needs-gpt` Ready/In Progress lane. Its prompt has the same hard completion gate: after any commit it must push, create/update/verify the PR with `gh pr view`, and final with a PR URL or `Stuck: {reason}`.

## Weekly Escalated Audit Cron

One combined cron handles all tracked repos via sub-agent spawning.

Script:
- None (no Python script — audit logic lives in the cron prompt and sub-agents)

Schedule:
- `(Saffron): Weekly Audit of Misospace by GPT-5.5` — `0 1 * * 3` @ America/Edmonton = 1am Wednesday MT

Behavior:
- Reads `TRACKED_REPOS` from `project_groom.py` to get the repo list (dynamically, not hardcoded)
- Spawns one audit sub-agent per tracked repo in parallel using `sessions_spawn`
- Each sub-agent independently audits its repo, opens a GitHub issue, and delivers results
- Sub-agents run with `openai-codex/gpt-5.5`, `thinking: high`, `timeout: 7200s`
- Parent cron times out after 300s (it just spawns; sub-agents run to completion independently)

Tracked repos (auto-synced from `project_groom.py`):
- misospace/miso-chat
- misospace/miso-gallery
- misospace/dispatch
- misospace/pr-reviewer-action
- misospace/windowstead

Note: Adding a repo to `project_groom.py`'s `TRACKED_REPOS` automatically includes it in the next audit cycle. No cron edit needed.