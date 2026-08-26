"""What the census promises about itself.

Three of these lock a lesson rather than a behaviour, and the lessons cost
something to learn: an instrument that scored zero tokens and printed OK,
committed inside the script whose whole job is catching that; and an allowlist
that quietly absorbs the finding it was supposed to record.
"""
import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import wb_decode_census as census        # noqa: E402


# The census's own tallies, 2026-08-26, 889,050 tokens over 98 ACs. Every gid
# here is on the allowlist for a reason recorded beside it in the script.
BASELINE_GIDS = {35, 193, 196, 198, 201, 211, 214}


def _result(**over):
    """A scan result with nothing wrong in it, for a test to spoil one field."""
    base = {"acs": 1, "tokens": 100_000, "seconds": 1.0,
            "hits": {"repha": collections.Counter(),
                     "mark": collections.Counter()},
            "where": {}, "acs_by_form": {}, "leading": {}}
    base.update(over)
    base["hits"] = {k: collections.Counter(v) for k, v in base["hits"].items()}
    return base


def test_zero_tokens_can_never_be_a_pass():
    """The false green this script was written to catch, caught in the script.

    The first run imported west_bengal_shreelipi but not west_bengal, whose
    import-time patch is what makes pdfplumber hand back glyph ids at all.
    Every page extracted as unmapped characters, nothing matched, and the
    census printed OK over nothing. The import is fixed; this test locks the
    other half, which is the half that generalizes to the next instrument.
    """
    out = census.main.__globals__
    saved = out["scan"]
    out["scan"] = lambda *a, **k: _result(tokens=0)
    try:
        assert census.main([]) == 1
    finally:
        out["scan"] = saved


def test_an_allowlist_row_must_cite_an_issue_or_name_the_source():
    census.assert_every_entry_is_cited()          # the real table
    census.assert_every_entry_is_cited({7: (1e-5, census.SOURCE_NOISE, "x")})
    census.assert_every_entry_is_cited({7: (1e-5, "#123", "x")})
    with pytest.raises(AssertionError, match="file it and cite the number"):
        census.assert_every_entry_is_cited({7: (1e-5, "known, harmless", "x")})


def test_the_allowlist_has_not_grown_since_the_baseline_was_measured():
    """Deliberately brittle. Adding a gid here is the thing to make noticeable.

    An allowlist's failure mode is not that it is wrong; it is that widening it
    is the cheapest way to make a red check green, and the widening looks like
    housekeeping in a diff. If this test is failing because you added a gid,
    the census found a leading glyph nobody had looked at — which is exactly
    what it is for. File it, cite the issue in the entry, and update this set
    in the same commit so the two statements cannot drift.
    """
    assert set(census.ALLOWED_LEADING_GIDS) == BASELINE_GIDS


def test_a_tracked_defect_and_a_damaged_glyph_read_differently_in_the_report():
    _, notes = census.verdict(_result(leading={211: 1, 214: 1}))
    joined = "\n".join(notes)
    assert "tracked defect #34" in joined
    assert "source noise" in joined


def test_a_gid_on_the_allowlist_still_fails_once_it_is_over_its_ceiling():
    ceiling = census.ALLOWED_LEADING_GIDS[214][0]
    under = _result(leading={214: 1}, tokens=int(2 / ceiling))
    over = _result(leading={214: 1}, tokens=int(0.5 / ceiling))
    assert census.verdict(under)[0] == []
    assert any("over its" in f for f in census.verdict(over)[0])


def test_a_word_initial_repha_fails_with_no_allowlist_to_appeal_to():
    """Marks have a baseline; the repha family has none, on purpose."""
    failures, _ = census.verdict(_result(hits={"repha": {"র্ক": 1}, "mark": {}}))
    assert any("seating bug" in f for f in failures)
