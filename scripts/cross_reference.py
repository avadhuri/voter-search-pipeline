"""
"Search old, match new" -- given one 2002-roll record, find likely matches
for the same person in the current roll.

Deliberately a one-record-at-a-time lookup (called from a single UI action
on a single search result), not a bulk auto-linkage across the whole roll --
that keeps this a verification aid for a caseworker rather than something
that reads as mass profiling.

Matching signal: same AC, same gender, name + relative-name fuzzy-scored,
and age advanced by the number of years between roll_year and the current
roll's year (with a small tolerance, since ages in this data are
self-reported/rounded).
"""
from matching import get_scorer, score_fields

AGE_TOLERANCE = 5


def find_candidates(conn, source_record, target_roll_year, algorithm, min_score=60, limit=10):
    """
    source_record: sqlite3.Row (or dict) for the 2002-roll record being checked.
    conn: sqlite3 connection to a DB that also contains target_roll_year rows
          for the same ac_code.
    Returns (candidates, note) where note explains an empty result (e.g. no
    current-roll data loaded yet for this AC) so the UI isn't a silent blank.
    """
    years_elapsed = target_roll_year - source_record["roll_year"]
    expected_age = (source_record["age"] or 0) + years_elapsed

    rows = conn.execute(
        """
        SELECT * FROM voters
        WHERE ac_code = ? AND roll_year = ? AND gender = ?
          AND age BETWEEN ? AND ?
        """,
        (
            source_record["ac_code"], target_roll_year, source_record["gender"],
            expected_age - AGE_TOLERANCE, expected_age + AGE_TOLERANCE,
        ),
    ).fetchall()

    if not rows:
        any_current = conn.execute(
            "SELECT COUNT(*) FROM voters WHERE ac_code = ? AND roll_year = ?",
            (source_record["ac_code"], target_roll_year),
        ).fetchone()[0]
        if any_current == 0:
            return [], (
                f"No {target_roll_year} roll data loaded yet for "
                f"{source_record['ac_code']} -- run the current-roll pilot/download first."
            )
        return [], "No current-roll voters in this AC matched on gender + expected age range."

    scorer = get_scorer(algorithm)
    scored = []
    for r in rows:
        score = score_fields(
            scorer,
            [source_record["full_name"], source_record["full_relative_name"]],
            [r["full_name"], r["full_relative_name"]],
        )
        if score is not None and score >= min_score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit], None
