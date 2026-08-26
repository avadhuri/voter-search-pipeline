"""
Shared, selectable name-matching scorers.

Both the CLI (search.py) and the web app (app.py) import ALGORITHMS from
here, so the README's algorithm write-up and the actual scoring code can't
drift apart.

Two scorers are shipped, chosen deliberately rather than blended into one
number:

- "wratio" (rapidfuzz.fuzz.WRatio): a composite token-based ratio. It tries
  several string-comparison strategies (plain ratio, token sort, token set,
  partial ratio) and returns the best, so it's tolerant of reordered tokens
  ("Kumar Ravi" vs "Ravi Kumar") and of one name being a substring of the
  other (a shortened/abbreviated entry). Good default for messy, OCR-era
  transliterations where word order isn't reliable.

- "jaro_winkler" (rapidfuzz.distance.JaroWinkler): a character-edit-distance
  metric purpose-built for short strings like personal names, standard in
  record-linkage literature. It scores based on matching characters within
  a bounded window plus transpositions, then boosts the score further for a
  shared prefix -- so "Shivaram" vs "Shivram" scores higher than an
  edit-distance metric would suggest, because the first several characters
  agree. Weaker than WRatio on reordered tokens (it compares the strings
  as-is, no token shuffling).

Neither is silently "better" -- they fail differently, which is why both are
exposed as an explicit user choice instead of averaged into a single score.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from rapidfuzz import fuzz, process, utils
from rapidfuzz.distance import JaroWinkler


def _wratio_score(query, candidate):
    # WRatio's partial-ratio strategy treats a short candidate as trivially
    # "contained" in a longer query -- a single character that merely
    # appears somewhere in the query name scores ~90 regardless of
    # relevance. That shows up in the source data as OCR-truncated
    # one/two-letter name entries outranking genuine matches. Below that
    # length, "candidate is a legitimate abbreviation of the query" (the
    # premise partial-ratio relies on) doesn't hold, so fall back to plain
    # ratio, which scores on the full strings with no such shortcut.
    processed = utils.default_process(candidate) if candidate else ""
    if len(processed) < 3:
        return fuzz.ratio(query, candidate, processor=utils.default_process)
    return fuzz.WRatio(query, candidate, processor=utils.default_process)


def _jaro_winkler_score(query, candidate):
    return JaroWinkler.normalized_similarity(
        utils.default_process(query), utils.default_process(candidate)
    ) * 100


def _wratio_cdist(query, candidates, workers=1):
    """Vectorized equivalent of calling _wratio_score(query, c) for every c
    in candidates -- same short-candidate fallback to plain ratio, just
    computed as one batch instead of a Python loop."""
    candidates = [c or "" for c in candidates]
    scores = process.cdist(
        [query], candidates, scorer=fuzz.WRatio,
        processor=utils.default_process, workers=workers, dtype=np.float32,
    )[0]
    short_idx = [
        i for i, c in enumerate(candidates)
        if len(utils.default_process(c) if c else "") < 3
    ]
    if short_idx:
        short_candidates = [candidates[i] for i in short_idx]
        short_scores = process.cdist(
            [query], short_candidates, scorer=fuzz.ratio,
            processor=utils.default_process, workers=workers, dtype=np.float32,
        )[0]
        scores[short_idx] = short_scores
    return scores


def _jaro_winkler_cdist(query, candidates, workers=1):
    candidates = [c or "" for c in candidates]
    return process.cdist(
        [query], candidates, scorer=JaroWinkler.normalized_similarity,
        processor=utils.default_process, score_multiplier=100,
        workers=workers, dtype=np.float32,
    )[0]


ALGORITHMS = {
    "wratio": {
        "label": "WRatio (composite, token-based)",
        "score": _wratio_score,
        "batch_score": _wratio_cdist,
    },
    "jaro_winkler": {
        "label": "Jaro-Winkler (prefix-weighted, record-linkage standard)",
        "score": _jaro_winkler_score,
        "batch_score": _jaro_winkler_cdist,
    },
}

DEFAULT_ALGORITHM = "wratio"


def get_scorer(algorithm):
    if algorithm not in ALGORITHMS:
        raise ValueError(
            f"Unknown algorithm '{algorithm}'. Choose one of: {', '.join(ALGORITHMS)}"
        )
    return ALGORITHMS[algorithm]["score"]


def get_batch_scorer(algorithm):
    if algorithm not in ALGORITHMS:
        raise ValueError(
            f"Unknown algorithm '{algorithm}'. Choose one of: {', '.join(ALGORITHMS)}"
        )
    return ALGORITHMS[algorithm]["batch_score"]


def score_fields(scorer, query_fields, record_fields):
    """
    query_fields / record_fields: parallel lists, e.g. [name_query, relative_query]
    and [record_full_name, record_full_relative_name]. Empty query fields are
    skipped; the final score is the mean over fields actually queried.
    """
    parts = []
    for q, r in zip(query_fields, record_fields):
        if q:
            parts.append(scorer(q, r or ""))
    if not parts:
        return None
    return sum(parts) / len(parts)


def score_fields_batch(batch_scorer, query_fields, record_field_lists, workers=1):
    """Vectorized equivalent of calling score_fields(scorer, query_fields,
    record_fields) once per record -- same "mean over non-empty query
    fields" semantics, computed as one array instead of a Python loop.

    query_fields: e.g. [name_query, relative_query]
    record_field_lists: parallel lists of per-record values, e.g.
    [record_names, record_relatives], each the same length (one entry per
    record). Returns a float32 ndarray of per-record scores; NaN where no
    query field was set (score_fields' `None` case has no batch-array
    equivalent, so this is the array analogue -- check with np.isnan).
    """
    n = len(record_field_lists[0]) if record_field_lists else 0
    total = np.zeros(n, dtype=np.float64)
    count = 0
    for q, records in zip(query_fields, record_field_lists):
        if q:
            total += batch_scorer(q, records, workers)
            count += 1
    if count == 0:
        return np.full(n, np.nan, dtype=np.float32)
    return (total / count).astype(np.float32)


def score_fields_batch_by_group(batch_scorer, query_fields, groups, max_workers=None):
    """Vectorized batch scoring, parallelized across groups (one group per
    constituency in practice) rather than within a single cdist call.

    Why this exists, not just `score_fields_batch(..., workers=N)`: rapidfuzz's
    own `cdist(workers=N)` parallelizes by handing out *query rows* to its
    internal thread pool (confirmed against rapidfuzz's C++ source,
    `process_cpp.hpp`'s `run_parallel`/`cdist_two_lists_impl` -- not assumed).
    A name search is always exactly one query row against many candidates, so
    `workers=N` there dispatches a single unit of work no matter what N is --
    real multi-core parallelism for this scorer, structurally unreachable via
    that parameter for this access pattern. The scorer does release the GIL
    for the whole call regardless of `workers`, though, so real concurrency
    has to be driven from outside rapidfuzz -- one `cdist(workers=1)` call per
    group, fanned out over a plain ThreadPoolExecutor. Measured against a real
    1.64M-row/5-AC tier: flat ~1170ms regardless of rapidfuzz's own
    `workers=1/4/-1`, vs. ~280-390ms via this grouped approach (4-8 threads) --
    identical output, confirmed via np.allclose.

    **That measurement is one search at a time, and the win does not survive
    a busy instance.** Splitting into per-group calls costs ~7% by itself;
    the fan-out repays it only out of cores nothing else wants. On the
    deployed shape (4 vCPU, 5 ACs, 1.65M rows) grouped runs 1.50x flat with
    one search in flight, 1.19x with two, and **0.78x with four** -- and no
    `max_workers` recovers that, w=1 being 0.83x there. The caller therefore
    owns the decision: this function parallelizes when asked and is a net
    cost when the cores are already spoken for. scripts/bench_concurrency.py
    has the full table and rules out the two obvious culprits (pool size,
    the GIL).

    groups: ordered mapping of {group_key: record_field_lists}, e.g.
    {(state, ac_code): [record_names, record_relatives]} -- one entry per
    constituency. A single-group call (e.g. a one-AC search) gets no
    parallelism from this function by design -- there's only one cdist call
    to run either way; that case is unaffected by this change.

    Returns (scores_by_group, timing_by_group_ms): scores_by_group is
    {group_key: float32 ndarray}, each in the same shape score_fields_batch
    would return for that group's rows; timing_by_group_ms is
    {group_key: elapsed_ms} for surfacing per-constituency query-time stats
    (e.g. in /dbg's timing breakdown).
    """
    keys = list(groups.keys())
    scores_by_group = {}
    timing_by_group_ms = {}
    if not keys:
        return scores_by_group, timing_by_group_ms

    def _score_one(key):
        t0 = time.perf_counter()
        scores = score_fields_batch(batch_scorer, query_fields, groups[key], workers=1)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return key, scores, elapsed_ms

    workers = max_workers or (os.cpu_count() or 1)
    workers = max(1, min(workers, len(keys)))

    if workers == 1:
        for key in keys:
            _, scores, elapsed_ms = _score_one(key)
            scores_by_group[key] = scores
            timing_by_group_ms[key] = elapsed_ms
        return scores_by_group, timing_by_group_ms

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for key, scores, elapsed_ms in ex.map(_score_one, keys):
            scores_by_group[key] = scores
            timing_by_group_ms[key] = elapsed_ms
    return scores_by_group, timing_by_group_ms
