"""
One-off audit: scan every data/raw/*.csv row-by-row and catalog every way a
row deviates from the "normal" 13-column shape / expected ac_code / expected
ac_name / expected district / numeric fields / known relation & gender codes.

Does NOT assume uniform format -- reports counts per anomaly type plus a
handful of sample rows for each, so parse_raw() can be updated to handle
every quirk actually observed rather than a guessed subset.
"""
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "data", "raw")
AC_META_PATH = os.path.join(BASE_DIR, "states", "meta", "ac_meta.json")

KNOWN_RELATIONS = {"F", "H", "M", "O", ""}
KNOWN_GENDERS = {"M", "F", "O", ""}
MAX_SAMPLES = 8


def main():
    with open(AC_META_PATH, encoding="utf-8") as f:
        ac_meta = {row["ac_code"]: row for row in json.load(f)}

    anomaly_counts = Counter()
    anomaly_samples = defaultdict(list)
    per_file_col_counts = defaultdict(Counter)
    per_file_ac_codes = defaultdict(Counter)
    per_file_districts = defaultdict(Counter)
    per_file_ac_names = defaultdict(Counter)
    total_rows = 0
    files_seen = 0

    def sample(kind, filename, lineno, cols):
        anomaly_counts[kind] += 1
        if len(anomaly_samples[kind]) < MAX_SAMPLES:
            anomaly_samples[kind].append((filename, lineno, cols))

    files = sorted(f for f in os.listdir(RAW_DIR) if f.endswith(".csv"))
    for filename in files:
        expected_ac_code = filename[:-4]  # e.g. "A083"
        expected_meta = ac_meta.get(expected_ac_code)
        path = os.path.join(RAW_DIR, filename)
        files_seen += 1
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        for lineno, cols in enumerate(reader, start=1):
            if not cols or (len(cols) == 1 and not cols[0].strip()):
                sample("blank_line", filename, lineno, cols)
                continue
            total_rows += 1
            ncols = len(cols)
            per_file_col_counts[filename][ncols] += 1

            if ncols < 13:
                sample(f"too_few_cols_{ncols}", filename, lineno, cols)
                continue
            if ncols > 13:
                trailing = cols[13:]
                if any(c.strip() for c in trailing):
                    sample(f"extra_nonempty_cols_{ncols}", filename, lineno, cols)
                else:
                    sample(f"extra_empty_cols_{ncols}", filename, lineno, cols)

            core = cols[:13]
            (district, ac_code, ac_name, part_no, serial_no, local_ref,
             first_name, first_suffix, relative_name, relative_suffix,
             relation_code, age, gender) = [((c or "").strip()) for c in core]

            per_file_ac_codes[filename][ac_code] += 1
            per_file_districts[filename][district] += 1
            per_file_ac_names[filename][ac_name] += 1

            norm_ac_code = ac_code if ac_code.upper().startswith("A") else "A" + ac_code.zfill(3)
            if expected_meta and norm_ac_code != expected_ac_code:
                sample("ac_code_mismatch", filename, lineno, cols)

            if expected_meta and ac_name and ac_name.strip().lower() != expected_meta["ac_name"].strip().lower():
                sample("ac_name_mismatch", filename, lineno, cols)

            if expected_meta and district and district.strip().lower() != expected_meta["district"].strip().lower():
                sample("district_mismatch", filename, lineno, cols)

            if not first_name:
                sample("empty_first_name", filename, lineno, cols)

            for label, val in (("part_no", part_no), ("serial_no", serial_no), ("age", age)):
                if val == "" or val.upper() == "NULL":
                    sample(f"empty_{label}", filename, lineno, cols)
                else:
                    try:
                        int(val)
                    except ValueError:
                        sample(f"non_numeric_{label}", filename, lineno, cols)

            if relation_code.upper() not in KNOWN_RELATIONS:
                sample("unknown_relation_code", filename, lineno, cols)

            if gender.upper() not in KNOWN_GENDERS:
                sample("unknown_gender", filename, lineno, cols)

            if any("�" in c for c in core):
                sample("decode_replacement_char", filename, lineno, cols)

        if files_seen % 25 == 0:
            print(f"...{files_seen}/{len(files)} files, {total_rows} rows so far", file=sys.stderr)

    print(f"\n=== TOTAL: {total_rows} rows across {files_seen} files ===\n")

    print("--- Anomaly counts ---")
    for kind, count in sorted(anomaly_counts.items(), key=lambda x: -x[1]):
        print(f"{kind}: {count}")

    print("\n--- Files with >1 distinct ac_code value ---")
    for filename, ctr in per_file_ac_codes.items():
        if len(ctr) > 1:
            print(f"{filename}: {dict(ctr)}")

    print("\n--- Files with >1 distinct district value ---")
    for filename, ctr in per_file_districts.items():
        if len(ctr) > 1:
            print(f"{filename}: {dict(ctr)}")

    print("\n--- Files with >1 distinct ac_name value ---")
    for filename, ctr in per_file_ac_names.items():
        if len(ctr) > 1:
            print(f"{filename}: {dict(ctr)}")

    print("\n--- Files with non-13 column counts ---")
    for filename, ctr in per_file_col_counts.items():
        if set(ctr.keys()) != {13}:
            print(f"{filename}: {dict(ctr)}")

    print("\n--- Samples per anomaly kind ---")
    for kind, samples in anomaly_samples.items():
        print(f"\n[{kind}] ({anomaly_counts[kind]} total)")
        for filename, lineno, cols in samples:
            print(f"  {filename}:{lineno}  {cols}")

    with open(os.path.join(BASE_DIR, "data", "audit_report.json"), "w") as f:
        json.dump({
            "total_rows": total_rows,
            "files_seen": files_seen,
            "anomaly_counts": dict(anomaly_counts),
            "files_multi_ac_code": {k: dict(v) for k, v in per_file_ac_codes.items() if len(v) > 1},
            "files_multi_district": {k: dict(v) for k, v in per_file_districts.items() if len(v) > 1},
            "files_multi_ac_name": {k: dict(v) for k, v in per_file_ac_names.items() if len(v) > 1},
            "files_nonstandard_colcount": {k: dict(v) for k, v in per_file_col_counts.items() if set(v.keys()) != {13}},
        }, f, indent=2)
    print("\nWrote data/audit_report.json")


if __name__ == "__main__":
    main()
