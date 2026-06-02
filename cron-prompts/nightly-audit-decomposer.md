NIGHTLY AUDIT DECOMPOSER - DETERMINISTIC ONLY

You are running the deterministic audit decomposer workflow.

Rules:
- Do not use GPT or LLM fallback.
- Do not involve the escalated worker.
- Do not hand-roll GitHub or Dispatch JSON.
- Run exactly this command:

```bash
python3 {{WORKFLOW_DIR}}/scripts/audit_decompose.py --scan --apply
```

The script scans tracked repos for open audit umbrella issues with parseable
`## Recommended issue breakdown` or `## Top findings` sections, creates or
updates child issues by stable marker, updates the parent `## Decomposed into`
block, and marks the umbrella decomposed in Dispatch.

Report the command output and stop.
