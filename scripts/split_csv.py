"""
Split a combined state CSV into per-AC CSV files for the CsvConnector pipeline.

Reads csv_output/<state>/<state>_combined.csv, groups rows by AC, and writes
one CSV per AC to csv_output/<state>/per_ac/<ac_no>.csv.  Each per-AC file
keeps the same header as the combined file.

Streams the input so memory stays flat regardless of file size (MP is 6+ GB).

Usage:
    python scripts/split_csv.py --state madhya_pradesh
    python scripts/split_csv.py --state lakshadweep
    python scripts/split_csv.py --all                    # all states with a combined CSV
"""
import argparse
import csv
import json
import os
import sys

CSV_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "csv_output")
META_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "states", "meta")

# Column name used for AC number varies by state
AC_COLUMNS = ["ac_no", "ac_number"]


def _detect_ac_column(fieldnames):
    """Return the AC-number column name present in this CSV's header."""
    for col in AC_COLUMNS:
        if col in fieldnames:
            return col
    raise ValueError(f"No AC column found in header. Expected one of {AC_COLUMNS}, "
                     f"got: {fieldnames}")


def split_state(state_id):
    combined = os.path.join(CSV_BASE, state_id, f"{state_id}_combined.csv")
    if not os.path.exists(combined):
        print(f"  SKIP {state_id}: no combined CSV at {combined}")
        return

    out_dir = os.path.join(CSV_BASE, state_id, "per_ac")

    # Load meta to know expected AC count
    meta_file = os.path.join(META_DIR, f"{state_id}_ac_meta.json")
    expected_acs = 0
    if os.path.exists(meta_file):
        with open(meta_file, encoding="utf-8") as f:
            expected_acs = len(json.load(f))

    # Skip only if per_ac/ has the right number of CSV files (not a partial split)
    if os.path.isdir(out_dir):
        existing = [f for f in os.listdir(out_dir) if f.endswith(".csv")]
        if expected_acs and len(existing) >= expected_acs:
            print(f"  SKIP {state_id}: already split ({len(existing)} ACs in {out_dir})")
            return
        elif existing:
            print(f"  {state_id}: partial split ({len(existing)}/{expected_acs} ACs), re-splitting...")
            for f in existing:
                os.remove(os.path.join(out_dir, f))

    os.makedirs(out_dir, exist_ok=True)

    # Stream through the file, writing rows to per-AC files
    writers = {}   # ac_no -> (file_handle, csv.writer)
    header = None
    ac_col = None
    total = 0
    ac_count = 0

    with open(combined, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        ac_col = _detect_ac_column(header)

        for row in reader:
            ac = row[ac_col].strip()
            if not ac:
                continue

            if ac not in writers:
                path = os.path.join(out_dir, f"{ac}.csv")
                f = open(path, "w", newline="", encoding="utf-8")
                w = csv.DictWriter(f, fieldnames=header)
                w.writeheader()
                writers[ac] = (f, w)
                ac_count += 1

            writers[ac][1].writerow(row)
            total += 1

            if total % 1_000_000 == 0:
                print(f"    {total:,} rows, {ac_count} ACs...", flush=True)

    # Close all file handles
    for f, _ in writers.values():
        f.close()

    print(f"  {state_id}: {total:,} rows split into {ac_count} per-AC files in {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Split combined CSVs into per-AC files")
    ap.add_argument("--state", default=None, help="state_id (e.g. madhya_pradesh)")
    ap.add_argument("--all", action="store_true", help="split all states with a combined CSV")
    args = ap.parse_args()

    if not args.state and not args.all:
        ap.print_help()
        sys.exit(1)

    if args.all:
        states = sorted(d for d in os.listdir(CSV_BASE)
                        if os.path.isfile(os.path.join(CSV_BASE, d, f"{d}_combined.csv")))
        print(f"Splitting {len(states)} states...")
        for state_id in states:
            split_state(state_id)
    else:
        split_state(args.state)


if __name__ == "__main__":
    main()
