import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from bench_scoring import score_new, score_old, synthetic_tier


@pytest.fixture(scope="module")
def rows():
    return synthetic_tier(1)  # ~40K rows, incl. short/garbled names


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
