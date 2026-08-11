"""
Bridges a Latin-script query against Devanagari-script source data (today,
just Haryana's DK-RAJ PDFs -- see states/haryana_dkraj.py -- which decode to
real Devanagari text, not Latin).

Uses indic_transliteration's rule-based Devanagari->ITRANS romanization
rather than a neural model (AI4Bharat's IndicXlit): IndicXlit's PyPI package
depends on `fairseq`, whose sdist has had a broken build (`fairseq/version.txt`
missing) for a long time -- three separate install strategies all failed in
this project's venv. `indic_transliteration` is pure Python, installs in
seconds, and against real Haryana top-name data it scored 80-100 on the
existing `wratio` scorer with zero changes to matching.py (e.g. "सुभाष" ->
"subhASha" vs. query "subhash" -> 93.3): WRatio already absorbs the ITRANS
scheme's artifacts (extra inherent vowels, capitalized retroflex markers) as
ordinary typos, so no separate phonetic algorithm is needed on top.

Precomputed once per distinct name string, not per query or per row: a
1.6M-row Haryana table has only ~183K distinct full_name/full_relative_name
values combined (names repeat heavily -- "सुनीता" alone appears 12K+ times),
and transliterating all of them takes about 3 seconds.
"""
import re

from indic_transliteration import sanscript

from states.registry import STATE_CONNECTORS

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

TRANSLIT_COLUMNS = ["full_name", "full_relative_name"]


def to_latin(devanagari_text):
    """Devanagari string -> romanized (ITRANS) string. Empty or already-Latin
    input passes through unchanged (cheap no-op for Karnataka/West Bengal
    rows if this is ever called on them)."""
    if not devanagari_text or not DEVANAGARI_RE.search(devanagari_text):
        return devanagari_text
    return sanscript.transliterate(devanagari_text, sanscript.DEVANAGARI, sanscript.ITRANS)


def is_latin_query(text):
    """True if text has no Devanagari codepoints, i.e. it should be matched
    against a Devanagari-script state's *_latin columns rather than its raw
    ones. A Devanagari query (like the demo terms in CLAUDE.md) still matches
    the raw columns directly, no transliteration involved."""
    return bool(text) and not DEVANAGARI_RE.search(text)


def devanagari_state_ids(state_ids=None):
    """Every registry state_id flagged script=devanagari, optionally
    restricted to a subset."""
    return [
        sid for sid, info in STATE_CONNECTORS.items()
        if info.get("script") == "devanagari" and (state_ids is None or sid in state_ids)
    ]


def ensure_latin_columns(conn):
    """ALTER TABLE ADD COLUMN for full_name_latin / full_relative_name_latin
    if they don't already exist -- needed for any DB built before this
    feature shipped. Idempotent; returns True if it changed the schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(voters)")}
    changed = False
    for column in TRANSLIT_COLUMNS:
        latin_column = f"{column}_latin"
        if latin_column not in existing:
            conn.execute(f"ALTER TABLE voters ADD COLUMN {latin_column} TEXT")
            changed = True
    return changed


def backfill_latin_columns(conn, state_ids=None, only_missing=True):
    """Populate *_latin columns for every Devanagari-script state's rows, one
    distinct string at a time via a temp-table join (not a per-row UPDATE --
    at 183K distinct strings that's the difference between ~3s and minutes).
    Returns the number of distinct strings transliterated. Caller commits."""
    target_states = devanagari_state_ids(state_ids)
    if not target_states:
        return 0

    state_placeholders = ",".join("?" * len(target_states))
    transliterated = 0

    for column in TRANSLIT_COLUMNS:
        latin_column = f"{column}_latin"
        missing_clause = f"AND {latin_column} IS NULL" if only_missing else ""
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM voters "
            f"WHERE state IN ({state_placeholders}) AND {column} IS NOT NULL "
            f"AND {column} != '' {missing_clause}",
            target_states,
        ).fetchall()
        distinct_values = [r[0] for r in rows]
        if not distinct_values:
            continue

        conn.execute("DROP TABLE IF EXISTS _translit_map")
        conn.execute("CREATE TEMP TABLE _translit_map (name TEXT PRIMARY KEY, latin TEXT)")
        conn.executemany(
            "INSERT INTO _translit_map (name, latin) VALUES (?, ?)",
            [(v, to_latin(v)) for v in distinct_values],
        )
        conn.execute(
            f"""
            UPDATE voters
            SET {latin_column} = (SELECT latin FROM _translit_map WHERE name = {column})
            WHERE state IN ({state_placeholders})
              AND {column} IN (SELECT name FROM _translit_map)
            """,
            target_states,
        )
        conn.execute("DROP TABLE _translit_map")
        transliterated += len(distinct_values)

    return transliterated


def backfill_latin_for_rows(conn, rows, devanagari_states):
    """Lazy-eval fallback for rows fetched mid-search whose *_latin columns
    are still NULL (a DB that hasn't had backfill_latin_columns() run against
    it yet, or a row added since the last run). Mutates `rows` (a list of
    dicts) in place and persists the computed values so the next search on
    the same names doesn't recompute them. Returns the number of rows
    touched."""
    updates = []
    for r in rows:
        if r["state"] not in devanagari_states:
            continue
        new_vals = {}
        for column in TRANSLIT_COLUMNS:
            latin_column = f"{column}_latin"
            if r.get(latin_column) is None and r.get(column):
                new_vals[latin_column] = to_latin(r[column])
        if new_vals:
            r.update(new_vals)
            updates.append((
                new_vals.get("full_name_latin"),
                new_vals.get("full_relative_name_latin"),
                r["id"],
            ))
    if updates:
        conn.executemany(
            "UPDATE voters SET "
            "full_name_latin = COALESCE(?, full_name_latin), "
            "full_relative_name_latin = COALESCE(?, full_relative_name_latin) "
            "WHERE id = ?",
            updates,
        )
        conn.commit()
    return len(updates)
