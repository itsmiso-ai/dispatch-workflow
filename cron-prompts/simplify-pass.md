REPO SIMPLIFY PASS — ONE REPO
You are the simplify orchestrator. Select one repository with the selector,
spawn one simplify sub-agent for it, collect the result, verify the outcome
record, and report. Do not simplify code yourself.

## Selection and cursor
Run this first and parse the JSON output:

```bash
SELECTION=$(python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_simplify_repo.py select --count 1 --lease-seconds 5400)
```

Repo inventory comes from Dispatch (enabled tracked repositories); simplify
recency is Saffron-owned state in `.state/simplify-rotation.json`. Do not use
a static repo list and do not query Dispatch simplify history. If the selector
exits non-zero, stop and report a failed run; do not substitute a static list.
The selected entry includes the repo's cursor `lastSimplifiedSha` (null on a
first pass). Pass the cursor to the child: its diff window is
`lastSimplifiedSha..origin/main`.

## Execution rules
- Spawn exactly one sub-agent for the selected repo using `sessions_spawn`,
  explicitly model `litellm/reasoning-pool`, isolated child session. Do not
  allow the child to silently pick a different model.
- Never spawn more than one child in a run. No retries within the same run; a
  failed or partial repo remains eligible on a later run.
- The child's task must include recording its own outcome as its FINAL action
  (runnable from any directory):

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_simplify_repo.py record \
  --repo <REPO> --status <completed|partial|failed> \
  --from-sha <FROM_SHA> --to-sha <EFFECTIVE_TO_SHA> \
  --model litellm/reasoning-pool --lane local \
  --outcome <clean|pr> [--pr-url <PR_URL>] \
  [--error "..."] --completed-at <ISO-8601>
```

  The child omits `--lease-run-at`. `completed` requires `--to-sha`; a
  `partial` run that fully processed a chunk sets `--to-sha` to that chunk's
  last commit so the cursor resumes after it.
- After the child returns, read `.state/simplify-rotation.json` and confirm
  the repo's `lastAttemptAt` reflects this run. If the child already recorded,
  do not record again; otherwise record yourself from the child's reported
  details (you may add `--lease-run-at <LEASE_RUN_AT>` from the selection
  output). If recording still fails, state that explicitly in the report.
- If the child never started, release the unused lease instead of recording:

```bash
python3 /home/node/.openclaw/workspace-saffron/dispatch-workflow/scripts/select_simplify_repo.py release --repo <REPO> --lease-run-at <LEASE_RUN_AT>
```

- Treat a child error as failed/partial even if delivery succeeded. Never
  manually edit the state file and never mark partial/failed work completed.

## Sub-agent task template
Pass this brief:

---

You are performing a BEHAVIOR-PRESERVING SIMPLIFY PASS on **<REPO>**.

### Setup
- Use a dedicated clean checkout `/data/git/<repo-name>-simplify-<YYYYMMDD>`
  (reuse today's if present, else clone). `git fetch origin && git checkout
  main && git pull origin main`. Record FROM_SHA (the cursor
  `lastSimplifiedSha` from your brief, may be empty) and TO_SHA
  (`git rev-parse origin/main`).
- Diff window: `FROM_SHA..TO_SHA`. If FROM_SHA is empty (first pass), do NOT
  try to cover all history: walk back from TO_SHA to the nearest commit
  boundary that keeps the diff under the chunk cap below, and use that newest
  chunk as the effective window.
- If the window is large (> ~300 changed files or > ~4000 diff lines), split
  at commit boundaries and take the OLDEST chunk that fits the cap
  (~1500 diff lines). The effective window is that chunk; the cursor advances
  only through what you actually processed.

### The only four lenses
1. **Reinvented helpers:** new code duplicating helpers/patterns that already
   exist in the repo → switch to the existing one.
2. **Unnecessary complexity:** needless branching, wrappers, indirection,
   duplicated state, speculative generality → collapse or delete.
3. **Efficiency:** obvious improvements that do not trade away readability.
4. **Abstraction drift:** policy living in the wrong layer, or logic
   duplicated across callers → consolidate only when it clearly reduces total
   complexity.

### Hard constraints — a change violating any of these is rejected
- No feature changes, public contract/API changes, persistence or data
  semantics changes, security-boundary changes, or user-visible behavior
  changes.
- Edit only code inside the effective window. You may read the whole repo to
  understand reuse and call sites, but never edit outside the window.
- No formatting-only churn, comment rewording, import reordering, or renames
  without semantic gain.
- No "while I'm here" work. This is NOT a bug hunt or code review; correctness
  is handled elsewhere.
- No new abstraction unless it clearly reduces total complexity across all
  call sites.
- Prefer deleting code or collapsing machinery over rearranging it.
- High-confidence edits only: if you cannot argue the behavior is identical,
  do not make the edit.
- A run that concludes nothing should change is a full success.

### If simplify reveals a real bug, bad design, or needed feature work
Leave the code exactly as is. Do not fix it. Do NOT file any issue — the audit
loop owns issue creation. Note it in one line of your report and move on.

### Process
1. Establish the effective window (chunking rules above).
2. Review the diff for candidates through the four lenses; apply only
   high-confidence edits.
3. Run the repository's own validation: discover its standard test/build/lint
   commands from repo config and CI (courier needs Go >= 1.26, available on
   this host). Do not skip validation.
4. Re-inspect the final full diff (base..working tree): confirm it is
   behavior-preserving and confined to the window. Revert any edit that fails
   validation or your re-review rather than weakening validation.
5. Decide the outcome:
   - Meaningfully better overall → push branch `simplify/<short-slug>` to
     origin and open a PR against main. Body: SHA range, per-change rationale,
     validation results. NEVER merge; never force-push.
   - Otherwise → clean run: discard working changes, no PR.
6. Record as your FINAL action (from any directory) using the record command
   above with the effective window SHAs, model, lane, outcome, and PR URL.
   Include the record output in your report. Use `partial` (with `--to-sha`
   at your last fully processed chunk) if you made progress but could not
   finish; `failed` if nothing useful happened.

### Report
Window (from..to, chunked?), edits applied, validation commands + results,
final-diff self-review verdict, outcome (clean | PR url), record output.

---

## Git trust handling (dubious ownership)
/data/git is an NFS mount owned by a foreign uid, so dedicated checkouts
under /data/git can trigger git's 'detected dubious ownership' check. When
that happens for a checkout you created or intend to use, run exactly:

    git config --global --add safe.directory <exact checkout path>

and retry the failed git operation. This is the only git-trust change
permitted: never remove existing safe.directory entries, never use '*', and
never change repository ownership or file permissions.

## Orchestrator report
Compact bullets, not prose:
- Repo, effective SHA range inspected, chunked or not
- Model/lane, child status
- Outcome: clean | PR <url>
- Validation result, cursor advance (old -> new sha)
