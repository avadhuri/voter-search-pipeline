"""
Fuzzy search over an ingested Karnataka voter-roll SQLite DB (see
build_db.py). Matching is delegated to scripts/matching.py's algorithm
registry -- the same registry the web app uses, so results here and in the
UI are always consistent for a given --algorithm choice.

CLI usage:
    search.py <db_path> --name "someone" [--relative "relative name"]
              [--ac A085 [A012 ...]] [--part N] [--gender M|F]
              [--age N --age-tolerance 3]
              [--algorithm wratio|jaro_winkler]
              [--min-score 70] [--limit 20]
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from matching import ALGORITHMS, DEFAULT_ALGORITHM, get_scorer, score_fields

MAX_ACS = 5


def load_rows(db_path, ac_codes=None, part_no=None, gender=None,
              age=None, age_tolerance=3):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM voters WHERE 1=1"
    params = []
    if ac_codes:
        if len(ac_codes) > MAX_ACS:
            raise ValueError(f"At most {MAX_ACS} ACs may be selected at once.")
        q += f" AND ac_code IN ({','.join('?' * len(ac_codes))})"
        params += list(ac_codes)
    if part_no is not None:
        q += " AND part_no = ?"
        params.append(part_no)
    if gender:
        q += " AND gender = ?"
        params.append(gender)
    if age is not None:
        q += " AND age BETWEEN ? AND ?"
        params += [age - age_tolerance, age + age_tolerance]
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search(rows, name=None, relative=None, algorithm=DEFAULT_ALGORITHM,
           min_score=70, limit=20):
    scorer = get_scorer(algorithm)
    scored = []
    for r in rows:
        score = score_fields(
            scorer,
            [name, relative],
            [r["full_name"], r["full_relative_name"]],
        )
        if score is not None and score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def print_results(results):
    if not results:
        print("No matches.")
        return
    for score, r in results:
        print(
            f"[{score:5.1f}] {r['ac_code']:<6} Part {r['part_no']:>4} Serial {r['serial_no']:>4} | "
            f"{r['full_name']:<25} {r['relation_label'] or '?':<10} of "
            f"{r['full_relative_name']:<25} | Age {r['age']} {r['gender']}"
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path")
    ap.add_argument("--name")
    ap.add_argument("--relative")
    ap.add_argument("--ac", nargs="+", help=f"Restrict to up to {MAX_ACS} AC codes, e.g. A085 A012")
    ap.add_argument("--part", type=int, default=None)
    ap.add_argument("--gender", choices=["M", "F"], default=None)
    ap.add_argument("--age", type=int, default=None)
    ap.add_argument("--age-tolerance", type=int, default=3)
    ap.add_argument("--algorithm", choices=list(ALGORITHMS), default=DEFAULT_ALGORITHM)
    ap.add_argument("--min-score", type=float, default=70)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if not args.name and not args.relative:
        ap.error("Provide --name and/or --relative")

    rows = load_rows(
        args.db_path, ac_codes=args.ac, part_no=args.part, gender=args.gender,
        age=args.age, age_tolerance=args.age_tolerance,
    )
    results = search(
        rows, name=args.name, relative=args.relative, algorithm=args.algorithm,
        min_score=args.min_score, limit=args.limit,
    )
    print_results(results)
