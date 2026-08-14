"""
Extract Bihar old SIR (2003) voter roll PDFs to CSV.

Bihar PDFs are HTML-to-PDF with ruled tables (wkhtmltopdf), so
pdfplumber.extract_tables() works directly. 14 columns:
  AC_NO, PART_NO, SL_NO_IN_PART, HOUSE_NO, SECTION_NO,
  FIRST_NAME, LAST_NAME, RLN_TYPE, RLN_FM_NM, RLN_L_NM,
  IDCARD_NO, PARTLINKNO, SEX, AGE

Names are in Devanagari with some (cid:XX) placeholders for unmapped glyphs.

Usage:
    python scripts/extract_bihar.py                    # all downloaded ACs
    python scripts/extract_bihar.py --ac 1,2,3         # specific ACs
    python scripts/extract_bihar.py --limit 5          # first 5 ACs
    python scripts/extract_bihar.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "bihar"
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "section_no",
    "first_name", "last_name", "elector_name",
    "relation", "relation_first_name", "relation_last_name", "relation_name",
    "epic_no", "sex", "age",
]


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def extract_pdf_table(pdf_bytes):
    """Extract rows from a Bihar PDF using table detection."""
    all_rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 14:
                        continue
                    # Skip header rows
                    if row[0] and ("AC_NO" in str(row[0]).upper() or "PART" in str(row[0]).upper()):
                        continue
                    # Data row: AC_NO should be numeric
                    ac_val = (row[0] or "").strip()
                    sl_val = (row[2] or "").strip()
                    if not sl_val or not sl_val.replace(".", "").isdigit():
                        continue
                    all_rows.append([c.strip() if c else "" for c in row[:14]])
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Extract Bihar SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/bihar")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    args = ap.parse_args()

    raw_dir = os.path.join("data", "raw", STATE_ID)
    os.makedirs(args.out_dir, exist_ok=True)

    meta = _load_meta()
    ac_map = {ac["ac_no"]: ac for ac in meta}

    zip_files = sorted([f for f in os.listdir(raw_dir) if f.endswith(".zip")])
    if args.ac:
        wanted = set(int(x) for x in args.ac.split(","))
        zip_files = [f for f in zip_files if int(re.search(r"\d+", f).group()) in wanted]
    if args.limit:
        zip_files = zip_files[:args.limit]

    print(f"{STATE_ID}: extracting {len(zip_files)} ACs")

    all_rows = []
    for zf in zip_files:
        zip_path = os.path.join(raw_dir, zf)
        ac_no = int(re.search(r"\d+", zf).group())
        ac_info = ac_map.get(ac_no, {})
        ac_name = ac_info.get("ac_name", "")
        district = ac_info.get("district_name", "")

        print(f"  {zf}: AC{ac_no:03d} {ac_name}...", end=" ", flush=True)

        with zipfile.ZipFile(zip_path) as z:
            part_files = sorted([n for n in z.namelist() if n.endswith(".pdf")])
            ac_rows = []
            for pf in part_files:
                m = re.search(r"(\d+)", os.path.basename(pf))
                if not m:
                    continue
                part_no = int(m.group(1))
                pdf_data = z.read(pf)
                rows = extract_pdf_table(pdf_data)
                for row in rows:
                    # row: [ac_no, part_no, sl_no, house, section, first, last,
                    #        rln_type, rln_fm, rln_ln, idcard, partlink, sex, age]
                    first_name = row[5]
                    last_name = row[6]
                    elector_name = (first_name + " " + last_name).strip()
                    rln_first = row[8]
                    rln_last = row[9]
                    relation_name = (rln_first + " " + rln_last).strip()
                    age = row[13].replace(".0", "") if row[13] else ""

                    full_row = [
                        STATE_ID, district, ac_no, ac_name, part_no,
                        row[2],  # serial_no
                        row[3],  # house_no
                        row[4],  # section_no
                        first_name, last_name, elector_name,
                        row[7],  # relation type
                        rln_first, rln_last, relation_name,
                        row[10],  # epic/idcard
                        row[12],  # sex
                        age,
                    ]
                    ac_rows.append(full_row)

        if args.combined:
            all_rows.extend(ac_rows)
        else:
            csv_path = os.path.join(args.out_dir, f"AC{ac_no:03d}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADERS)
                w.writerows(ac_rows)

        print(f"{len(ac_rows)} rows")

    if args.combined:
        csv_path = os.path.join(args.out_dir, f"{STATE_ID}_combined.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADERS)
            w.writerows(all_rows)
        print(f"Combined: {csv_path} ({len(all_rows)} rows)")
    else:
        print(f"Per-AC CSVs in {args.out_dir}/")


if __name__ == "__main__":
    main()
