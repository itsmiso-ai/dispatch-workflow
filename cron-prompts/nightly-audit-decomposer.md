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

The script scans tracked repos for open audit umbrella issues with a parseable
`## Recommended Issue Breakdown` section (one `### [Pn] Title` block per
recommendation — the sole source of child issues), creates or updates child
issues by stable marker, updates the parent `## Decomposed into` block, and
marks the umbrella decomposed in Dispatch. Other sections (`## Top Findings`,
etc.) are human context and are not parsed.

Report the command output and stop.
