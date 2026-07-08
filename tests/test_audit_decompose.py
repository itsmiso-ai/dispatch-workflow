"""Tests for the deterministic audit decomposer.

The decomposer parses a weekly-audit umbrella issue's `## Recommended Issue
Breakdown` into one child issue per recommendation. A recommendation is fully
self-contained: nothing outside the breakdown section may leak into a child
(the regression that stapled whole `## Top Findings` priority buckets into
every child — see test_no_top_findings_leak)."""

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
