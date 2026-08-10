import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from eval_embeddings import build_dataset, evaluate_scorer, POOL_SIZE


def test_build_dataset_shapes_are_consistent():
    queries = build_dataset()
    assert len(queries) > 0
    for q in queries:
        assert len(q["candidates"]) == POOL_SIZE
        assert q["true_name"] in q["candidates"]
        assert q["query"] != ""


def test_evaluate_scorer_perfect_scorer_gets_recall_1():
    queries = build_dataset()[:20]

    def exact_match_scorer(query, candidate):
        return 1.0 if candidate == queries[0]["true_name"] else 0.0

    # A scorer that always prefers the true answer for query 0 should rank
    # it first for query 0 specifically -- sanity check the metric plumbing
    # rather than any real matching quality.
    result = evaluate_scorer(queries[:1], lambda q, c: 1.0 if c == queries[0]["true_name"] else 0.0)
    assert result["recall@1"] == 1.0
    assert result["mrr"] == 1.0


def test_evaluate_scorer_handles_ties_without_crashing():
    queries = build_dataset()[:20]

    result = evaluate_scorer(queries, lambda q, c: 0.0)
    for k in ("recall@1", "recall@3", "recall@5", "mrr"):
        assert 0.0 <= result[k] <= 1.0
