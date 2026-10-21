TECH DEBT / OPTIMIZATION AUDIT — ONE REPO (DIRECT)

You are the audit orchestrator. You select one repository with the selector,
audit it yourself in this turn, record the outcome, and report. Do not spawn
sub-agents and do not delegate the audit.

## Audit selection and recency
Run this command first and parse its JSON output. The command must run in
the workflow checkout so the ignored `.state` directory is shared by all
phase jobs:

```bash
SELECTION=$(python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_audit_repos.py select --count 1 --lease-seconds 5400)
```

The selector queries Dispatch for the enabled repository inventory. Audit
recency is Saffron-owned state in `.state/audit-rotation.json`; do not query
Dispatch audit history and do not use a static repo list. The selector
bootstraps an empty state file from existing direct-finding issues carrying
the `audit` label (historical umbrella issues are ignored). `.repos[0]` is
the single repository for this run. If the selector exits non-zero, stop and
report a failed run; do not substitute a static list.

## Dispatch semantics
- Use `DISPATCH_URL` and `DISPATCH_AGENT_TOKEN` only for Dispatch API access
  needed.
- Do not use legacy `MISSION_CONTROL_*` variables.
- GitHub Projects are deprecated. Do not read or mutate project boards.
- Do not close issues automatically.

## Execution
- Audit the selected repository yourself, in this turn. Never spawn
  sub-agents for the audit.
- No retries within the same run; a failed or partial repo remains eligible
  for the next run.

## Audit brief
You are auditing **<REPO>** for tech debt, security issues, and optimization
opportunities. This is a recurring audit — focus on actionable findings.

### What to inspect
- Current code (structure, quality, obvious debt)
- Open issues (avoid duplicating existing work)
- Open PRs and recent merged PRs (regressions, loose ends)
- Dependencies (outdated, vulnerable, missing)
- Test coverage gaps
- Configuration and deployment concerns

### Issue creation rules
For each finding, create a single GitHub issue directly in the repo. Use the
workflow's safe create wrapper rather than calling `gh issue create`
directly:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/create_audit_issue.py \
  --repo <REPO> --title "<TITLE>" --body-file <BODY_FILE> \
  --run-id "$AUDIT_RUN_ID" --label audit --label status/backlog --label priority/p{n}
```

The wrapper serializes creates, checks open and closed issues by normalized
exact title, records the result in `.state/audit-create-ledger.json`, and
rechecks GitHub after a failed or timed-out request before permitting any
retry. Set `AUDIT_RUN_ID` to the selector lease timestamp. A closed match is
still a duplicate unless there is concrete evidence of regression; do not
use `--allow-regression` for ordinary audit findings.

1. **Dedup first.** Before creating, search existing open and recently
   closed issues with specific keywords
   (`gh issue list --search "<keywords>" --state all`). An issue covering
   the same behavior or files is a duplicate even when its title differs. A
   closed issue is still a duplicate unless there is concrete evidence of
   regression. If a create request fails or times out, search by exact
   title before retrying; never blindly submit the same issue again.
2. **Title:** concise, action-oriented. Prefix with `[Pn]` where n is 0-3
   (P0 = critical/security, P1 = high, P2 = medium, P3 = low).
3. **Body must include:**
   - `Ask: <one imperative sentence>` — one concrete imperative sentence on
     its own line near the top of the issue body. Keep it cleanly quotable;
     do not hedge or combine multiple asks.
   - `Expected files: <paths>` — concrete, repo-relative paths the fix is
     expected to touch. Only list paths supported by the finding's evidence;
     do not speculate. If no path can be named confidently, do not file the
     finding.
   - `**Problem:**` — what is wrong and why it matters
   - `**Evidence:**` — file paths, line refs, commands proving it
   - `**Acceptance:**` — concrete, checkable done criteria
4. **Required issue-body shape:** put the two required lines before
   `**Problem:**`, for example:

   ```text
   Ask: Dedupe incidents on the group signature so a re-fire updates the existing issue instead of creating a new one.
   Expected files: internal/delivery/github.go, internal/triage/group.go
   ```

   Every issue filed by this audit must contain both lines. The autonomous
   review loop quotes the ask verbatim and checks whether the expected paths
   overlap the eventual diff; missing or speculative fields can cause valid
   work to be rejected.
5. **Labels:** `audit`, `status/backlog`, and `priority/p{n}`. Create labels
   if they don't exist (`gh label create` with appropriate color/description).
6. **Do not create an umbrella issue.** Each finding is its own issue.

### Guidelines
- Prefer 3-7 high-quality findings over 15 shallow ones.
- P0/P1 findings should be things that would actually cause problems if
  ignored.
- P2/P3 findings are cleanup and improvement opportunities.
- Apply an actionability gate before filing. If a finding is not worth
  doing, is merely speculative, or has no concrete acceptance criteria,
  discard it. Do not file it, mention it in an issue comment, or include a
  "not worth doing" section in the report.

## Output
Report back:
- Number of issues created, by priority
- Number of existing issues found (deduped)
- Anything you could not complete (and why)

## Failure and recording rules
- Do not start any retry work in this run. Keep the run result auditable and
  leave failed or partial repos eligible for the next run.
- Treat an error as a failed or partial audit even if Discord delivery
  succeeds.
- Record the result by its exact repo and final status: `completed`,
  `partial`, `failed`, or `skipped`. Include the error text for failures.
- If you created issues and then hit an error, the run is `partial`, not
  `completed`. Verify the issue activity on GitHub before describing the
  outcome; never assume an error means that no issues were filed.
- When partial, preserve the issue numbers and findings already verified.
  The next run must dedupe against those issues and audit the remaining
  surface area rather than blindly repeating the whole scan.
- Record with the selector (use your completion timestamp when available):

  ```bash
  python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_audit_repos.py record --repo <REPO> --status <completed|partial|failed|skipped> [--error "..."] [--completed-at <ISO-8601>] [--lease-run-at <LEASE_RUN_AT>]
  ```

  Use `completed` only when the audit finished normally. Record `partial`
  when issues were created but the run then errored. Record `failed` when no
  useful audit completed. Record `skipped` only when explicitly skipped.
- Record a result even when the run errors. After recording, read
  `.state/audit-rotation.json` and confirm the selected repo's
  `lastAttemptAt` reflects this run; if recording failed, state that
  explicitly in the final report. Advance `lastSuccessfulAuditAt` only for
  `completed`; do not manually edit the state file and never mark
  partial/failed work successful.
- If the run must abort before any audit work happens, release the unused
  lease instead of recording a successful audit:

  ```bash
  python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_audit_repos.py release --repo <REPO> --lease-run-at <LEASE_RUN_AT>
  ```

## Report
Compact bullets, not prose:
- The selected repo: issues created, deduped, and errors
- The lease timestamp and final status
- No retries are attempted in this mode
- Use an overall `partial`/`failed` result when the audit did not complete;
  a delivered report alone is not an `ok` audit result

## Git trust handling (dubious ownership)
/data/git is an NFS mount owned by a foreign uid, so dedicated audit
checkouts under /data/git can trigger git's 'detected dubious ownership'
check. When that happens for a checkout you created or intend to use for
this audit, run exactly:

    git config --global --add safe.directory <exact checkout path>

and retry the failed git operation. This is the only git-trust change
permitted: never remove existing safe.directory entries, never use '*', and
never change repository ownership or file permissions.
