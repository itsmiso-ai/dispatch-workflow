"""Tests for the deterministic audit decomposer.

The decomposer parses a weekly-audit umbrella issue's `## Recommended Issue
Breakdown` into one child issue per recommendation. A recommendation is fully
self-contained: nothing outside the breakdown section may leak into a child
(the regression that stapled whole `## Top Findings` priority buckets into
every child — see test_no_top_findings_leak)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_decompose as A  # noqa: E402


def _issue(body: str, number: int = 9) -> A.Issue:
    return A.Issue(
        number=number,
        title="Weekly tech debt audit: demo - 2026-07-01",
        body=body,
        state="open",
        html_url=f"https://github.com/misospace/demo/issues/{number}",
        labels=[],
    )


# --- v2 strict contract: one `### [Pn] Title` block per issue ---

V2_BODY = """## Summary
Some prose.

## Top Findings

### P1 — High
1. **Unrelated finding about widgets** — evidence in widgets.py
2. **Unrelated finding about gadgets** — evidence in gadgets.py

## Recommended Issue Breakdown

### [P1] Implement tag persistence

**Problem:** Tags from /tag are only logged, never stored.

**Evidence:** app.py /tag (~L590) logs then returns ok; no write.

**Acceptance:** tags persist across restart; test covers round-trip.

### [P2] Extract thumbnail cache cleanup

**Problem:** Cleanup logic duplicated across five delete paths.

**Acceptance:** single batch-purge function; all callers updated.

## Not Worth Doing Yet
- nothing here
"""


def test_v2_blocks_parse_one_candidate_each():
    cands, source = A.parse_candidates("misospace/demo", _issue(V2_BODY))
    assert source == "breakdown"
    assert [(c.title, c.priority) for c in cands] == [
        ("Implement tag persistence", 1),
        ("Extract thumbnail cache cleanup", 2),
    ]


def test_v2_child_body_is_self_contained():
    cands, _ = A.parse_candidates("misospace/demo", _issue(V2_BODY))
    body = A.child_body("misospace/demo", _issue(V2_BODY), cands[0])
    assert "## Recommendation" in body
    assert "**Problem:**" in body and "**Acceptance:**" in body
    # The block heading is the issue title, not repeated in the body.
    assert "[P1]" not in body


def test_no_top_findings_leak():
    """The bug this replaces: a child's body contained the whole matched
    priority bucket. No content from `## Top Findings` may appear in any child."""
    parent = _issue(V2_BODY)
    cands, _ = A.parse_candidates("misospace/demo", parent)
    for cand in cands:
        body = A.child_body("misospace/demo", parent, cand).lower()
        assert "widget" not in body
        assert "gadget" not in body
        assert "matched top finding" not in body


# --- legacy fallback: numbered `**Pn — Title**` list (pre-contract umbrellas) ---

LEGACY_BODY = """## Recommended Issue Breakdown

1. **P1 — Implement tag persistence: store tags as sidecar files or SQLite**
   Add a storage backend. Update /tag and /api/llm/tags to persist.
2. **P2 — Extract thumbnail cache cleanup into a single batch function**
   Consolidate the duplicated removal loops.

## Not Worth Doing Yet
- nothing
"""


def test_legacy_numbered_list_still_parses():
    cands, source = A.parse_candidates("misospace/demo", _issue(LEGACY_BODY))
    assert source == "breakdown"
    assert len(cands) == 2
    assert cands[0].priority == 1
    assert "tag persistence" in cands[0].title.lower()
    assert cands[1].priority == 2


# --- regression (misospace/windowstead#269 -> bogus child #273) ---
# An old-format umbrella uses an H3 '### Recommended issue breakdown' heading with
# a numbered list, and a sibling '### Not worth doing yet' H3. Because the section
# boundary is H2-only, that prose H3 is swept into the breakdown section. It must
# NOT become the sole child (dropping every real finding) — a '###' block without
# a [Pn] marker is prose, so the numbered fallback still runs.

WINDOWSTEAD_269_BODY = """## Summary
Healthy mid-extraction state.

### Recommended issue breakdown

1. **P1 — Audit-decompose cron idempotency** — respect the decompose marker.
2. **P2 — recruit_worker name pool collision** — 11th recruit reuses a name.

### Not worth doing yet

Carried forward from #221 — still valid:
- Full pixel-art asset pipeline
- Web export / network integration

## Decomposed into
<!-- audit-decompose:v1 -->
<!-- /audit-decompose:v1 -->
"""


def test_h3_prose_block_does_not_suppress_numbered_fallback():
    parent = _issue(WINDOWSTEAD_269_BODY)
    cands, source = A.parse_candidates("misospace/demo", parent)
    assert source == "breakdown"
    titles = [c.title.lower() for c in cands]
    # The swept-in prose heading must never become a child.
    assert not any("not worth doing" in t for t in titles), titles
    # The real numbered findings decompose via the legacy fallback.
    assert len(cands) == 2
    assert cands[0].priority == 1
    assert "idempotency" in cands[0].title.lower()
    assert cands[1].priority == 2
    # No prose from the swept-in block leaks into a child body.
    for cand in cands:
        body = A.child_body("misospace/demo", parent, cand).lower()
        assert "pixel-art" not in body
        assert "not worth doing" not in body


# --- unit: priority splitting ---

def test_split_priority_heading_variants():
    assert A.split_priority_heading("[P1] Implement tags") == (1, "Implement tags")
    assert A.split_priority_heading("P2 — Do the thing") == (2, "Do the thing")
    assert A.split_priority_heading("P3 - Another") == (3, "Another")
    assert A.split_priority_heading("No priority here") == (None, "No priority here")


# --- section boundary: H3 blocks stay inside their H2 section ---

def test_extract_section_includes_h3_children():
    section = A.extract_section(V2_BODY, "Recommended issue breakdown")
    assert section is not None
    assert "### [P1] Implement tag persistence" in section
    assert "### [P2] Extract thumbnail cache cleanup" in section
    # Stops at the next H2, does not swallow the following section.
    assert "Not Worth Doing Yet" not in section


def test_missing_breakdown_yields_no_candidates():
    cands, source = A.parse_candidates("misospace/demo", _issue("## Summary\nnothing here\n"))
    assert cands == []
    assert source == "none"


# --- dedup: repeated titles collapse to one child ---

def test_duplicate_titles_deduped():
    body = """## Recommended Issue Breakdown

### [P1] Fix the flaky test

**Problem:** it flakes.

### [P2] Fix the flaky test

**Problem:** duplicate title, different priority.
"""
    cands, _ = A.parse_candidates("misospace/demo", _issue(body))
    assert len(cands) == 1
    assert cands[0].priority == 1  # first occurrence wins


# --- the enrichment heuristics are gone for good ---

def test_removed_enrichment_symbols_absent():
    for name in ("extract_top_findings", "match_top_finding", "significant_tokens"):
        assert not hasattr(A, name), f"{name} should be deleted"


# --- auto-close: umbrella closes once all its decomposed children are done ---

UMBRELLA_DONE = """## Summary
audit prose.

## Decomposed into
<!-- audit-decompose:v1 -->
- #101 — First finding
- #102 — Second finding
<!-- /audit-decompose:v1 -->
"""


def _umbrella(body, number=9, title="Weekly tech debt audit: demo - 2026-07-01"):
    return {"number": number, "title": title, "body": body}


class _FakeGH:
    """Stub for A.gh_json: returns child states by number, records mutations."""

    def __init__(self, child_states):
        self.child_states = child_states  # {number: "open"|"closed"}
        self.calls = []  # (method, path, payload)

    def __call__(self, path, *, method="GET", payload=None, query=None):
        self.calls.append((method, path, payload))
        m = re.search(r"/issues/(\d+)$", path)
        if method == "GET" and m:
            return {"number": int(m.group(1)), "state": self.child_states[int(m.group(1))]}
        return {}


def test_decomposed_child_numbers_parses_block():
    assert A.decomposed_child_numbers(UMBRELLA_DONE) == [101, 102]
    assert A.decomposed_child_numbers("## Summary\nno block here\n") == []


def test_close_completed_umbrella_closes_when_all_children_closed(monkeypatch):
    fake = _FakeGH({101: "closed", 102: "closed"})
    monkeypatch.setattr(A, "gh_json", fake)
    rc = A.close_completed_umbrella("misospace/demo", _umbrella(UMBRELLA_DONE), apply=True)
    assert rc == 1
    method_paths = [(m, p) for (m, p, _) in fake.calls]
    assert ("POST", "/repos/misospace/demo/issues/9/comments") in method_paths
    patch = [c for c in fake.calls if c[0] == "PATCH" and c[1] == "/repos/misospace/demo/issues/9"]
    assert patch and patch[0][2].get("state") == "closed"


def test_close_completed_umbrella_keeps_open_when_a_child_is_open(monkeypatch):
    fake = _FakeGH({101: "closed", 102: "open"})
    monkeypatch.setattr(A, "gh_json", fake)
    rc = A.close_completed_umbrella("misospace/demo", _umbrella(UMBRELLA_DONE), apply=True)
    assert rc == 0
    assert not any(m == "PATCH" for (m, _, _) in fake.calls)


def test_close_completed_umbrella_never_closes_childless(monkeypatch):
    fake = _FakeGH({})
    monkeypatch.setattr(A, "gh_json", fake)
    empty = _umbrella("## Decomposed into\n<!-- audit-decompose:v1 -->\n<!-- /audit-decompose:v1 -->\n")
    assert A.close_completed_umbrella("misospace/demo", empty, apply=True) == 0
    assert fake.calls == []  # bails before any API call


def test_close_completed_umbrella_ignores_non_audit(monkeypatch):
    fake = _FakeGH({101: "closed", 102: "closed"})
    monkeypatch.setattr(A, "gh_json", fake)
    issue = _umbrella(UMBRELLA_DONE, title="Some feature request")
    assert A.close_completed_umbrella("misospace/demo", issue, apply=True) == 0
    assert fake.calls == []


def test_close_completed_umbrella_dry_run_makes_no_mutations(monkeypatch):
    fake = _FakeGH({101: "closed", 102: "closed"})
    monkeypatch.setattr(A, "gh_json", fake)
    rc = A.close_completed_umbrella("misospace/demo", _umbrella(UMBRELLA_DONE), apply=False)
    assert rc == 1
    assert not any(m in ("POST", "PATCH") for (m, _, _) in fake.calls)
