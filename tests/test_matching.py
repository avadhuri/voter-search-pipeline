import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from matching import (
    ALGORITHMS,
    get_batch_scorer,
    get_scorer,
    score_fields,
    score_fields_batch,
)


def test_both_algorithms_registered():
    assert "wratio" in ALGORITHMS
    assert "jaro_winkler" in ALGORITHMS


def test_exact_match_scores_100():
    for name in ALGORITHMS:
        scorer = get_scorer(name)
        assert scorer("Shivaram", "Shivaram") == 100


def test_wratio_tolerates_reordering():
    scorer = get_scorer("wratio")
    score = scorer("Kumar Ravi", "Ravi Kumar")
    assert score > 90


def test_jaro_winkler_rewards_shared_prefix():
    scorer = get_scorer("jaro_winkler")
    close_prefix = scorer("Shivaram", "Shivram")   # shares "Shiv" prefix, one dropped char
    far_prefix = scorer("Shivaram", "Aramshiv")     # same letters, no shared prefix
    assert close_prefix > far_prefix


def test_score_fields_skips_empty_query_fields():
    scorer = get_scorer("wratio")
    score = score_fields(scorer, ["Shivaram", ""], ["Shivaram", "anything"])
    assert score == 100


def test_score_fields_all_empty_returns_none():
    scorer = get_scorer("wratio")
    assert score_fields(scorer, ["", ""], ["a", "b"]) is None


NAMES = ["Shivaram", "Ravi Kumar", "Kumar Ravi", "Ab", "Sita Devi", "", "Shivram", None]
RELATIVES = ["Ramesh Rao", "", "Suresh", "Sh", None, "Geeta", "Shivram", "Anand"]


def test_batch_scoring_matches_per_row_scoring_exactly():
    """The whole point of the vectorized cdist path is identical output to
    the old per-row loop -- not an approximation."""
    for algo in ALGORITHMS:
        scorer = get_scorer(algo)
        batch_scorer = get_batch_scorer(algo)
        for query in ["Shivaram", "Ravi Kumar", "Kumar Ravi"]:
            expected = [
                score_fields(scorer, [query, ""], [n, r])
                for n, r in zip(NAMES, RELATIVES)
            ]
            actual = score_fields_batch(batch_scorer, [query, ""], [NAMES, RELATIVES])
            for e, a in zip(expected, actual):
                assert e == pytest.approx(float(a), abs=1e-3)


def test_batch_scoring_two_query_fields_matches_mean_of_per_row():
    scorer = get_scorer("wratio")
    batch_scorer = get_batch_scorer("wratio")
    expected = [
        score_fields(scorer, ["Ravi Kumar", "Anand"], [n, r])
        for n, r in zip(NAMES, RELATIVES)
    ]
    actual = score_fields_batch(batch_scorer, ["Ravi Kumar", "Anand"], [NAMES, RELATIVES])
    for e, a in zip(expected, actual):
        assert e == pytest.approx(float(a), abs=1e-3)


def test_score_fields_batch_all_empty_query_returns_nan():
    batch_scorer = get_batch_scorer("wratio")
    scores = score_fields_batch(batch_scorer, ["", ""], [NAMES, RELATIVES])
    assert np.all(np.isnan(scores))
