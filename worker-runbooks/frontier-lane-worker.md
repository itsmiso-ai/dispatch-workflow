# Frontier-Lane Worker

Lane: `frontier`.

Worker identity:

```bash
export DISPATCH_AGENT_NAME=saffron-frontier
```

Read first:
- `/home/node/.openclaw/workspace-saffron/dispatch-workflow/worker-runbooks/shared-dispatch-worker.md`

## Preflight

Run deterministic preflight before model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-frontier python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/dispatch_worker_preflight.py --lane frontier --claim --json
```

Preflight actions:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: update the returned PR only.
- `resume-active-work`: obey returned `nextAction` exactly.
- `claim-ready-issue`: implement returned claimed Ready issue.

## Queue Semantics

- Consume frontier lane only.
- Prefer active/checkpointed work for `saffron-frontier`.
- Otherwise pick the first unclaimed claimable `status/ready` frontier item.
- Do not consume `status/backlog`.
- Do not consume Renovate issues unless explicitly requested.
- If no claimable frontier work exists, end exactly:
  `Frontier lane is clear.`

## Audit / Umbrella Issues

**Do not decompose audit or umbrella issues.** Audit/umbrella decomposition
is handled by the weekly audit sub-agent + audit-decomposer workflow in the
dispatch-workflow repo. The frontier worker focuses on implementation
and design work only.

Skip any issue labeled `audit` or `needs-gpt` that is an umbrella/parent with
multiple findings.

## Valid Frontier Actions

- Implement one focused high-impact fix and open/update a PR.
- Write one concrete design/RFC comment when implementation is not safe yet.

## Final Forms

End exactly one:
- `Frontier lane is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`
