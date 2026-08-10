import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from matching import ALGORITHMS, get_scorer, score_fields


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
