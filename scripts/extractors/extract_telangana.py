"""
Extract Telangana old SIR (2002) voter roll PDFs to CSV.

Telangana PDFs have a multi-line format with no column-number markers:
  Line 1 (English): PS_No  Sl_No  Section_No  House_No  Age  EPIC_No
  Line 2 (Telugu):  Elector_Name  Rln_Type  Relation_Name  Gender

The approach: parse English lines for numeric/EPIC fields, Telugu lines
for names. Records span 2 lines each.

Usage:
    python scripts/extract_telangana.py                    # all downloaded ACs
    python scripts/extract_telangana.py --ac 188           # specific AC
    python scripts/extract_telangana.py --limit 3          # first 3 ACs
    python scripts/extract_telangana.py --combined         # single CSV
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "telangana"


def _group_into_rows(words, tolerance=5.0):
    """Group extracted PDF words into rows by vertical position."""
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    current_row = [words_sorted[0]]
    current_top = words_sorted[0]["top"]
    for w in words_sorted[1:]:
        if w["top"] - current_top <= tolerance:
            current_row.append(w)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [w]
            current_top = w["top"]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def _load_meta():
    """Load AC metadata JSON for Telangana."""
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "ps_no", "serial_no", "section_no", "house_no",
    "elector_name_telugu", "relation_telugu", "relation_name_telugu",
    "gender_telugu", "age", "epic_no",
]

# Telugu Unicode range
TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")


def is_telugu_line(words):
    """Check if a row of words contains Telugu script."""
    text = " ".join(w["text"] for w in words)
    return bool(TELUGU_RE.search(text))


def extract_telangana_page(page):
    """Extract records from one Telangana PDF page.

    Each record is 2 lines:
      English line: ps_no sl_no section_no house_no [age] [epic_no]
      Telugu line:  elector_name relation relation_name gender
    """
    words = page.extract_words()
    if not words:
        return []

    rows = _group_into_rows(words)
    records = []
    pending_english = None

    for row_words in rows:
        text = " ".join(w["text"] for w in row_words)

        # Skip header lines
        if "Assembly Constituency" in text or "PS No" in text or "Sl. No" in text:
            continue

        if is_telugu_line(row_words):
            # Telugu line — names, relation, gender
            if pending_english is not None:
                # Combine with previous English line
                telugu_parts = [w["text"] for w in row_words]
                telugu_text = " ".join(telugu_parts)
                pending_english["telugu"] = telugu_text
                records.append(pending_english)
                pending_english = None
        else:
            # English line — numeric fields
            tokens = text.split()
            # Look for pattern: ps_no sl_no section_no house_no [age] [epic_no]
            nums = [t for t in tokens if t.replace("-", "").replace("/", "").isdigit() or re.match(r"[A-Z]{2}\d+", t)]
            if len(nums) >= 2:
                # First few tokens are numeric fields
                parts = tokens
                ps_no = parts[0] if parts else ""
                sl_no = parts[1] if len(parts) > 1 else ""
                section_no = parts[2] if len(parts) > 2 else ""
                house_no = parts[3] if len(parts) > 3 else ""
                age = ""
                epic_no = ""
                # Age and EPIC are at the end
                for t in reversed(parts):
                    if re.fullmatch(r"[A-Z]{2}\d{9,}", t):
                        epic_no = t
                    elif re.fullmatch(r"\d{1,3}", t) and not age and t != ps_no and t != sl_no:
                        age = t

                if sl_no.isdigit():
                    pending_english = {
                        "ps_no": ps_no,
                        "sl_no": sl_no,
                        "section_no": section_no,
                        "house_no": house_no,
                        "age": age,
                        "epic_no": epic_no,
                        "telugu": "",
                    }

    return records


def extract_pdf(pdf_bytes):
    all_records = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            all_records.extend(extract_telangana_page(page))
    return all_records


def main():
    ap = argparse.ArgumentParser(description="Extract Telangana SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/telangana")
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
                records = extract_pdf(z.read(pf))
                for rec in records:
                    ac_rows.append([
                        STATE_ID, district, ac_no, ac_name, part_no,
                        rec["ps_no"], rec["sl_no"], rec["section_no"],
                        rec["house_no"], rec["telugu"], "", "", "",
                        rec["age"], rec["epic_no"],
                    ])

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
