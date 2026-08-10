"""
Benchmark: does a multilingual sentence-embedding model beat the
rapidfuzz/Jaro-Winkler baseline at matching OCR/transliteration-style
misspelled Indian names? Evaluation-only -- the embedding layer is shipped
in the app only if it wins here.

Setup (heavy deps, kept OUT of requirements.txt so the main app stays
light enough for a small VM):
    venv/bin/pip install -r requirements-eval.txt

Run:
    venv/bin/python scripts/eval_embeddings.py

Method: a labeled synthetic test set (real OCR/transliteration-era typo
patterns applied to a pool of real-sounding Indian names, since no real
mislabeled SIR pairs were available to build this on). Each query name is
searched against a candidate pool of size POOL_SIZE (1 true corrupted
match + distractors drawn from the same name pool); we report
Recall@1/@3/@5 and Mean Reciprocal Rank (MRR) for the two rapidfuzz
scorers already shipped (matching.py) vs. the embedding model, at
matched cost (same candidate pool, same k).
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from matching import get_scorer

POOL_SIZE = 20  # candidates per query: 1 true match + 19 distractors
NUM_QUERIES = 150
SEED = 42

FIRST_NAMES = [
    "Shivaram", "Ramaiah", "Lakshmi", "Venkatesh", "Krishnamurthy", "Manjula",
    "Nagaraj", "Vasanthi", "Siddaramaiah", "Puttaswamy", "Gangamma", "Chikkamma",
    "Basavaraj", "Shantamma", "Muniraju", "Yellappa", "Honnamma", "Doddamma",
    "Ravindra", "Sudha", "Prakash", "Geetha", "Mahadevaiah", "Nagamma",
    "Chandrashekar", "Rajamma", "Byrappa", "Kempamma", "Halappa", "Thimmaiah",
]
LAST_NAMES = [
    "Gowda", "Reddy", "Naik", "Setty", "Rao", "Iyer", "Hegde", "Shetty",
    "Naidu", "Pillai", "Achar", "Bhat", "Kumar", "Swamy", "Murthy",
]


def _delete_random_char(s):
    if len(s) < 5:
        return s
    i = random.randrange(1, len(s) - 1)
    return s[:i] + s[i + 1:]


def _swap_adjacent(s):
    if len(s) < 4:
        return s
    i = random.randrange(0, len(s) - 1)
    if s[i] == " " or s[i + 1] == " ":
        return s
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]


def _corrupt(name):
    """Apply several OCR/transliteration-era-style corruptions -- harder
    than a single typo, since real misreadings on scanned 2002-era rolls
    stack (bad scan + transliteration ambiguity + OCR segmentation)."""
    ops = [
        lambda s: s.replace("v", "w", 1) if "v" in s else s.replace("w", "v", 1),
        lambda s: s.replace("i", "y", 1) if "i" in s else s,
        lambda s: s.replace("th", "t", 1) if "th" in s else s,
        lambda s: s.replace("ph", "f", 1) if "ph" in s else s,
        lambda s: s.replace("a", "aa", 1) if "a" in s else s,
        lambda s: s.replace("m", "n", 1) if "m" in s else s.replace("n", "m", 1),
        lambda s: s.replace(" ", "", 1),  # OCR line-break merges the two words
        _delete_random_char,
        _swap_adjacent,
    ]
    random.shuffle(ops)
    corrupted = name
    for op in ops[:4]:  # stack four corruptions
        corrupted = op(corrupted)
    return corrupted


def build_dataset():
    random.seed(SEED)
    pool = [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]
    random.shuffle(pool)

    queries = []
    for i in range(NUM_QUERIES):
        true_name = pool[i % len(pool)]
        query = _corrupt(true_name)
        true_first, true_last = true_name.split(" ", 1)
        # Hard distractors: share a surname or given name with the true
        # match (very common in real voter rolls -- lots of "X Gowda"s) so
        # a method that only "sounds close" can't win on surname alone.
        hard_pool = [
            n for n in pool
            if n != true_name and (n.endswith(" " + true_last) or n.startswith(true_first + " "))
        ]
        random.shuffle(hard_pool)
        hard = hard_pool[: POOL_SIZE // 2]
        remaining = POOL_SIZE - 1 - len(hard)
        easy = random.sample(
            [n for n in pool if n != true_name and n not in hard], remaining
        )
        candidates = hard + easy + [true_name]
        random.shuffle(candidates)
        queries.append({"query": query, "true_name": true_name, "candidates": candidates})
    return queries


def evaluate_scorer(queries, score_fn):
    """score_fn(query, candidate) -> float. Returns recall@1/@3/@5 and MRR."""
    hits_at = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks = []

    for q in queries:
        scored = sorted(
            q["candidates"], key=lambda c: score_fn(q["query"], c), reverse=True
        )
        rank = scored.index(q["true_name"]) + 1
        for k in hits_at:
            if rank <= k:
                hits_at[k] += 1
        reciprocal_ranks.append(1.0 / rank)

    n = len(queries)
    return {
        "recall@1": hits_at[1] / n,
        "recall@3": hits_at[3] / n,
        "recall@5": hits_at[5] / n,
        "mrr": sum(reciprocal_ranks) / n,
    }


def run_rapidfuzz_baselines(queries):
    results = {}
    for name in ("wratio", "jaro_winkler"):
        scorer = get_scorer(name)
        results[name] = evaluate_scorer(queries, scorer)
    return results


def run_embedding_model(queries, model_name="paraphrase-multilingual-MiniLM-L12-v2"):
    try:
        from sentence_transformers import SentenceTransformer, util
    except ImportError:
        print(
            "sentence-transformers not installed — run "
            "`pip install -r requirements-eval.txt` first. Skipping embedding eval."
        )
        return None

    print(f"Loading {model_name} (first run downloads ~470MB)...")
    model = SentenceTransformer(model_name)

    all_names = sorted({q["query"] for q in queries} | {c for q in queries for c in q["candidates"]})
    embeddings = model.encode(all_names, convert_to_tensor=True, show_progress_bar=True)
    emb_index = {name: embeddings[i] for i, name in enumerate(all_names)}

    def score_fn(query, candidate):
        return util.cos_sim(emb_index[query], emb_index[candidate]).item()

    return evaluate_scorer(queries, score_fn)


def main():
    queries = build_dataset()
    print(f"Evaluating on {len(queries)} synthetic queries, pool size {POOL_SIZE}.\n")

    baseline_results = run_rapidfuzz_baselines(queries)
    embedding_results = run_embedding_model(queries)

    all_results = dict(baseline_results)
    if embedding_results is not None:
        all_results["embedding (MiniLM-L12-v2)"] = embedding_results

    header = f"{'method':<28}{'recall@1':>10}{'recall@3':>10}{'recall@5':>10}{'mrr':>10}"
    print(header)
    print("-" * len(header))
    for name, r in all_results.items():
        print(f"{name:<28}{r['recall@1']:>10.3f}{r['recall@3']:>10.3f}{r['recall@5']:>10.3f}{r['mrr']:>10.3f}")

    if embedding_results is not None:
        best_baseline_mrr = max(r["mrr"] for r in baseline_results.values())
        verdict = "WINS" if embedding_results["mrr"] > best_baseline_mrr else "does NOT win"
        print(
            f"\nVerdict: the embedding model {verdict} against the best rapidfuzz "
            f"baseline (MRR {embedding_results['mrr']:.3f} vs {best_baseline_mrr:.3f}). "
            + ("Ship it only if this margin holds up on real mislabeled SIR pairs, "
               "not just this synthetic set." if verdict == "WINS" else
               "Recommendation: do not ship — rapidfuzz is simpler, faster, and no "
               "heavier than a 470MB model download on every deploy.")
        )


if __name__ == "__main__":
    main()
