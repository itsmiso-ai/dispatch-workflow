# Cloud-Lane Worker

Lane: `cloud`.

Worker identity:

```bash
export DISPATCH_AGENT_NAME=saffron-cloud
```

Read first:
- `/home/node/.openclaw/workspace-saffron/dispatch-workflow/worker-runbooks/shared-dispatch-worker.md`

## Preflight

Run deterministic preflight before model judgment:

```bash
DISPATCH_AGENT_NAME=saffron-cloud python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/dispatch_worker_preflight.py --lane cloud --claim --json
```

Preflight actions:
- `clear` / `stuck`: reply with `terminal` exactly and stop.
- `pr-fix`: update the returned PR only.
- `resume-active-work`: obey returned `nextAction` exactly.
- `claim-ready-issue`: implement returned claimed Ready issue.

## Queue Semantics

- Consume cloud lane only.
- Prefer active/checkpointed work for `saffron-cloud`.
- Otherwise pick the first unclaimed claimable `status/ready` cloud item.
- Do not consume `status/backlog`.
- Do not consume Renovate issues unless explicitly requested.
- If no claimable cloud work exists, end exactly:
  `Cloud lane is clear.`

## Valid Cloud Actions

- Implement one focused multi-file fix and open/update a PR.
- Debug an issue requiring inference across multiple files or services.
- Write a concrete design comment when implementation is not safe yet.

## Final Forms

End exactly one:
- `Cloud lane is clear.`
- `Done. PR #{pr} opened for {repo} #{number}: {pr_url}.`
- `Done. PR #{pr} updated for {repo}: {pr_url}.`
- `Stuck: {reason}.`
