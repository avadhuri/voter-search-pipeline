"""
Benchmark: old per-row scoring loop vs. the vectorized rapidfuzz cdist path
(scripts/matching.py's score_fields_batch), across AC-count tiers.

Real numbers, not guesswork -- see CLAUDE.md/plan §1. Run directly:
    DB_PATH=data/db/multi_state_2002.sqlite venv/bin/python scripts/bench_scoring.py

Loads real rows via the same query shape _do_search uses (multiple
(state, ac_code) pairs OR'd together) when a built DB is available at
DB_PATH; falls back to synthetic ~200K-row data (with some short/garbled
<3-char names to exercise the WRatio short-candidate fallback branch) so
this runs without a built DB too, e.g. in CI.

Measured result (real DB, both a macOS dev machine and a Linux/Cloud-Run-
shaped `--cpus=4` container): ~1.2-1.7x speedup across the 5/10/25/28-AC
tiers, from vectorizing the scoring loop itself (Python per-row loop ->
one C-level rapidfuzz.process.cdist call). rapidfuzz's cdist(workers=N) is
documented as real multi-core parallelism, but was separately benchmarked
here against real (diverse) voter names on both platforms and showed no
reliable win from workers>1 on this workload -- an early synthetic
low-entropy-string microbenchmark suggested otherwise, but didn't hold up
against real data. See app.py's SCORING_WORKERS for the current default
(1) and that finding.
"""
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matching import ALGORITHMS, get_batch_scorer, get_scorer, score_fields, score_fields_batch

TIERS = [5, 10, 25, 28]  # AC counts: today's MAX_ACS, then up to WB's largest district (28)
SYNTHETIC_ROWS_PER_AC = 40_000  # ~ observed real per-AC scale (CLAUDE.md: 150-220K for 5 ACs)


def load_real_tier(db_path, n_acs):
    """Pick the n_acs largest real ACs by row count (worst-case scale, same
    shape as a district select-all) and fetch their rows via the exact
    multi-AC WHERE-clause shape _do_search uses."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    pairs = conn.execute(
        "SELECT state, ac_code, COUNT(*) c FROM voters GROUP BY state, ac_code "
        "ORDER BY c DESC LIMIT ?",
        (n_acs,),
    ).fetchall()
    if len(pairs) < n_acs:
        conn.close()
        return None
    ac_clause = " OR ".join(["(state = ? AND ac_code = ?)"] * len(pairs))
    params = [v for p in pairs for v in (p["state"], p["ac_code"])]
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, full_name, full_relative_name FROM voters WHERE ({ac_clause})", params
    ).fetchall()]
    conn.close()
    return rows


def synthetic_tier(n_acs, seed=42):
    rng = random.Random(seed)
    first_names = ["Ravi", "Sita", "Anand", "Geeta", "Suresh", "Lakshmi", "Manoj", "Kavita"]
    last_names = ["Kumar", "Devi", "Rao", "Sharma", "Reddy", "Nair", "Patel", "Gowda"]
    rows = []
    n_rows = n_acs * SYNTHETIC_ROWS_PER_AC
    for i in range(n_rows):
        if i % 500 == 0:
            # OCR-truncated one/two-letter entries -- exercises the
            # WRatio short-candidate fallback-to-ratio branch.
            name = rng.choice(["A", "Rm", "S", "Kj"])
        else:
            name = f"{rng.choice(first_names)} {rng.choice(last_names)}"
        relative = f"{rng.choice(first_names)} {rng.choice(last_names)}"
        rows.append({"id": i, "full_name": name, "full_relative_name": relative})
    return rows


def load_tier(n_acs, db_path):
    if db_path and os.path.exists(db_path):
        rows = load_real_tier(db_path, n_acs)
        if rows is not None:
            return rows, "real"
    return synthetic_tier(n_acs), "synthetic"


def score_old(rows, name, relative, algorithm):
    scorer = get_scorer(algorithm)
    scored = []
    for r in rows:
        score = score_fields(scorer, [name, relative], [r["full_name"], r["full_relative_name"]])
        if score is not None and score >= 70:
            scored.append((score, r["id"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:50]


def score_new(rows, name, relative, algorithm, workers=1):
    batch_scorer = get_batch_scorer(algorithm)
    names = [r["full_name"] for r in rows]
    relatives = [r["full_relative_name"] for r in rows]
    scores = score_fields_batch(batch_scorer, [name, relative], [names, relatives], workers=workers)
    scored = [
        (float(scores[i]), rows[i]["id"])
        for i in range(len(rows))
        if scores[i] == scores[i] and scores[i] >= 70  # scores[i]==scores[i] filters NaN
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[:50]


def run_tier(n_acs, db_path, name="Ravi Kumar", relative="Anand Sharma", algorithm="wratio"):
    rows, source = load_tier(n_acs, db_path)

    t0 = time.perf_counter()
    old_result = score_old(rows, name, relative, algorithm)
    old_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    new_result = score_new(rows, name, relative, algorithm)
    new_ms = (time.perf_counter() - t0) * 1000

    return {
        "n_acs": n_acs, "rows": len(rows), "source": source,
        "old_ms": old_ms, "new_ms": new_ms,
        "speedup": (old_ms / new_ms) if new_ms else float("inf"),
        "old_result": old_result, "new_result": new_result,
    }


def main():
    db_path = os.environ.get("DB_PATH", "data/db/multi_state_2002.sqlite")
    print(f"{'ACs':>5} {'rows':>10} {'source':>10} {'old ms':>10} {'new ms':>10} {'speedup':>9}")
    for n_acs in TIERS:
        r = run_tier(n_acs, db_path)
        print(f"{r['n_acs']:>5} {r['rows']:>10} {r['source']:>10} "
              f"{r['old_ms']:>10.1f} {r['new_ms']:>10.1f} {r['speedup']:>8.1f}x")


if __name__ == "__main__":
    main()
