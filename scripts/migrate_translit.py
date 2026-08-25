"""
One-time migration: backfill full_name_latin / full_relative_name_latin on
an already-built DB (e.g. data/db/multi_state_2002.sqlite), so Latin-script
search against a non-Latin-script state (Haryana, and every state added
since) doesn't rely solely on app.py's per-search lazy-eval fallback for its
first hit.

Safe to re-run -- only fills rows whose *_latin columns are still empty, so
an interrupted run just resumes, and a connector that supplied its own
romanization keeps it. See scripts/transliteration.py for why this is
precomputed per distinct string rather than per row or per query, and for
the per-script quality numbers that make a connector-supplied value worth
preferring.

Usage:
    scripts/migrate_translit.py [db_path]
        Defaults to $DB_PATH or data/db/multi_state_2002.sqlite.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from transliteration import backfill_latin_columns, ensure_latin_columns


def migrate(db_path):
    conn = sqlite3.connect(db_path)
    added_columns = ensure_latin_columns(conn)
    if added_columns:
        print("Added full_name_latin / full_relative_name_latin columns.")

    started = time.perf_counter()
    result = backfill_latin_columns(conn)
    conn.commit()
    elapsed = time.perf_counter() - started

    print(
        f"Transliterated {result.transliterated} distinct non-Latin name "
        f"strings in {elapsed:.1f}s."
    )
    if result.incomplete:
        examples = ", ".join(f"{native} -> {latin}" for native, latin in result.sample)
        print(
            f"  WARNING: {result.incomplete} still contain native-script "
            f"characters the transliteration scheme has no mapping for, so a "
            f"Latin-script query will score badly against them -- those "
            f"states want a connector-supplied full_name_latin. e.g. {examples}"
        )
    conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "data/db/multi_state_2002.sqlite")
    migrate(db_path)
