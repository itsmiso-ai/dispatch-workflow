# Subagent Patterns

On-demand reference for Saffron subagent work. Keep this file short enough to
load only when the `AGENTS.md` subagent rule fires.

## Model Compatibility

- MiniMax-M2.7 supports `thinking`: `off`, `minimal`, `low`, `medium`, `high`.
- Do not send `thinking: "adaptive"` to MiniMax-M2.7.
- If no specific thinking level is needed for MiniMax-M2.7, omit it or use
  `medium`.

## When To Spawn

Use subagents for bounded, parallel work where the parent can continue without
waiting on the result immediately.

Good uses:
- independent repo audits
- isolated code investigation
- one worker per disjoint implementation area
- verification that can run while local work continues

Avoid spawning for:
- a task you can finish directly in one pass
- work requiring tight interactive judgment
- anything whose result blocks the very next local action

## Prompt Shape

Give the worker:
- exact repo/path scope
- exact objective
- files/modules it owns
- validation expected
- final output format

For code changes, tell workers:
- edit files directly in their workspace
- do not revert unrelated edits
- list changed paths in the final response

## Dispatch Worker Notes

For Noelle/Varka style work, prefer deterministic local preflight before model
work:

```bash
DISPATCH_AGENT_NAME=saffron-normal python3 /home/node/.openclaw/workspace-saffron/scripts/dispatch_worker_preflight.py --lane normal --claim --json
DISPATCH_AGENT_NAME=saffron-escalated python3 /home/node/.openclaw/workspace-saffron/scripts/dispatch_worker_preflight.py --lane escalated --claim --json
```

Validate terminal worker text when practical:

```bash
printf '%s\n' "$FINAL_TEXT" | python3 /home/node/.openclaw/workspace-saffron/scripts/worker_result_guard.py
```
