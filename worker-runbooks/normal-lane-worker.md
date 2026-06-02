# Normal-Lane Worker

Lane: `normal`.

Worker identity:

```bash
export DISPATCH_AGENT_NAME=saffron-normal
```

Read first:
- `/home/node/.openclaw/workspace-saffron/dispatch-workflow/worker-runbooks/shared-dispatch-worker.md`

## Preflight

Run deterministic preflight before model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-normal python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/dispatch_worker_preflight.py --lane normal --claim --json
```

Preflight actions:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: update the returned PR only.
- `resume-active-work`: obey returned `nextAction` exactly.
- `claim-ready-issue`: implement returned claimed Ready issue.

## Queue Semantics

- Consume normal lane only.
- Prefer active/checkpointed work for `saffron-normal`.
- Otherwise pick the first unclaimed claimable `status/ready` item.
- Do not consume `status/backlog`.
- Do not consume Renovate issues unless explicitly requested.
- If no claimable work exists, end exactly: `Pipeline is clear.`

## Final Forms

End exactly one:
- `Pipeline is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`
