import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from bench_scoring import score_grouped, score_new, score_old, synthetic_tier


@pytest.fixture(scope="module")
def rows():
    return synthetic_tier(1)  # ~40K rows, incl. short/garbled names


@pytest.fixture(scope="module")
def multi_ac_rows():
    return synthetic_tier(5)  # 5 AC groups -- needed to exercise grouped concurrency


@pytest.mark.parametrize("algorithm", ["wratio", "jaro_winkler"])
def test_old_and_new_scoring_produce_identical_top50(rows, algorithm):
    """The vectorized cdist path must be an exact behavioral match for the
    old per-row loop, not an approximation -- same (score, voter_id) top-50,
    same order."""
    old = score_old(rows, "Ravi Kumar", "Anand Sharma", algorithm)
    new = score_new(rows, "Ravi Kumar", "Anand Sharma", algorithm)
    assert len(old) == len(new)
    for (old_score, old_id), (new_score, new_id) in zip(old, new):
        assert old_id == new_id
        assert old_score == pytest.approx(new_score, abs=1e-3)


def test_new_scoring_is_meaningfully_faster(rows):
    """Regression guard: the whole point of vectorizing is a real wall-clock
    win, not just equivalent output."""
    t0 = time.perf_counter()
    score_old(rows, "Ravi Kumar", "Anand Sharma", "wratio")
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    score_new(rows, "Ravi Kumar", "Anand Sharma", "wratio")
    new_ms = (time.perf_counter() - t0) * 1000

    assert new_ms < old_ms


@pytest.mark.parametrize("algorithm", ["wratio", "jaro_winkler"])
def test_grouped_and_flat_scoring_produce_identical_top50(multi_ac_rows, algorithm):
    """Grouping by constituency is purely a concurrency mechanism -- same
    top-50 as scoring everything in one flat call."""
    flat = score_new(multi_ac_rows, "Ravi Kumar", "Anand Sharma", algorithm)
    grouped, _timing = score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", algorithm)
    assert len(flat) == len(grouped)
    for (flat_score, flat_id), (grouped_score, grouped_id) in zip(flat, grouped):
        assert flat_id == grouped_id
        assert flat_score == pytest.approx(grouped_score, abs=1e-3)


def test_grouped_scoring_is_meaningfully_faster_across_constituencies(multi_ac_rows):
    """Only a multi-AC tier can show this -- score_fields_batch_by_group's
    concurrency is across groups, so a single-AC tier gets no speedup by
    design (see its docstring)."""
    t0 = time.perf_counter()
    score_new(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio")
    flat_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio")
    grouped_ms = (time.perf_counter() - t0) * 1000

    assert grouped_ms < flat_ms


def test_grouped_scoring_returns_per_constituency_timing(multi_ac_rows):
    _scored, timing_by_group_ms = score_grouped(multi_ac_rows, "Ravi Kumar", "Anand Sharma", "wratio")
    assert len(timing_by_group_ms) == 5
    assert all(ms >= 0 for ms in timing_by_group_ms.values())
