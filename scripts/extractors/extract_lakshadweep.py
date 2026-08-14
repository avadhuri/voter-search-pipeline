"""
Extract Lakshadweep old SIR (2002) voter roll PDFs to CSV.

PDF has ruled tables with 14 columns extractable via pdfplumber.extract_tables():
  District, AC_No, Island, Part, Serial, House, Elector_Name,
  Relation, Relation_Name, Gender_Local, Gender, Age, EPIC, Status

Names are in Malayalam with (cid:XXX) placeholders for unmapped glyphs.

Usage:
    python scripts/extractors/extract_lakshadweep.py
    python scripts/extractors/extract_lakshadweep.py --combined
"""
import argparse, csv, io, json, os, re, zipfile
import pdfplumber

STATE_ID = "lakshadweep"
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "gender_local", "sex", "age", "epic_no", "status",
]


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def extract_pdf_table(pdf_bytes):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 12:
                        continue
                    sl = (row[4] or "").strip()
                    if sl and sl.isdigit():
                        rows.append([str(c or "").strip() for c in row[:14]])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ac", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out-dir", default="output/csv/lakshadweep")
    ap.add_argument("--combined", action="store_true")
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
        ac_no = int(re.search(r"\d+", zf).group())
        ac_info = ac_map.get(ac_no, {})
        ac_name = ac_info.get("ac_name", "")
        district = ac_info.get("district_name", "")
        print(f"  {zf}: AC{ac_no:03d} {ac_name}...", end=" ", flush=True)

        with zipfile.ZipFile(os.path.join(raw_dir, zf)) as z:
            ac_rows = []
            for pf in sorted(n for n in z.namelist() if n.endswith(".pdf")):
                m = re.search(r"(\d+)", os.path.basename(pf))
                if not m: continue
                part_no = int(m.group(1))
                for row in extract_pdf_table(z.read(pf)):
                    # row: [dist, ac, island, part, serial, house, name, rel, rel_name, gender_l, sex, age, epic, status]
                    ac_rows.append([
                        STATE_ID, district, ac_no, ac_name, part_no,
                        row[4], row[5], row[6], row[7], row[8],
                        row[9], row[10], row[11],
                        row[12] if len(row) > 12 else "",
                        row[13] if len(row) > 13 else "",
                    ])

        if args.combined:
            all_rows.extend(ac_rows)
        else:
            with open(os.path.join(args.out_dir, f"AC{ac_no:03d}.csv"), "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)
                csv.writer(f).writerows(ac_rows)
        print(f"{len(ac_rows)} rows")

    if args.combined:
        path = os.path.join(args.out_dir, f"{STATE_ID}_combined.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)
            csv.writer(f).writerows(all_rows)
        print(f"Combined: {path} ({len(all_rows)} rows)")


if __name__ == "__main__":
    main()
