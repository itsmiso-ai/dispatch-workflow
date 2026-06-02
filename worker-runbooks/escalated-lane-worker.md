# Escalated-Lane Worker

Lane: `escalated`.

Worker identity:

```bash
export DISPATCH_AGENT_NAME=saffron-escalated
```

Read first:
- `/home/node/.openclaw/workspace-saffron/mission-control-workflow/worker-runbooks/shared-dispatch-worker.md`

## Preflight

Run deterministic preflight before model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-escalated python3 /home/node/.openclaw/workspace-saffron/mission-control-workflow/scripts/dispatch_worker_preflight.py --lane escalated --claim --json
```

Preflight actions:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: update the returned PR only.
- `resume-active-work`: obey returned `nextAction` exactly.
- `claim-ready-issue`: implement returned claimed Ready issue.

## Queue Semantics

- Consume escalated lane only.
- Prefer active/checkpointed work for `saffron-escalated`.
- Otherwise pick the first unclaimed claimable `status/ready` escalated item.
- Do not consume `status/backlog`.
- Do not consume Renovate issues unless explicitly requested.
- If no claimable escalated work exists, end exactly:
  `Escalated lane is clear.`

## Audit / Umbrella Issues

**Do not decompose audit or umbrella issues.** Audit/umbrella decomposition
is handled by the weekly audit sub-agent + audit-decomposer workflow in the
mission-control-workflow repo. The escalated worker focuses on implementation
and design work only.

Skip any issue labeled `audit` or `needs-gpt` that is an umbrella/parent with
multiple findings.

## Valid Escalated Actions

- Implement one focused high-impact fix and open/update a PR.
- Write one concrete design/RFC comment when implementation is not safe yet.

## Final Forms

End exactly one:
- `Escalated lane is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`
