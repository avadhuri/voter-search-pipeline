"""
Extract Dadra & Nagar Haveli and Daman & Diu old SIR voter roll PDFs to CSV.

PDF layout: 8 columns
  1. Serial No  2. House No  3. Elector Name  4. Relation (F/H/M/O)
  5. Relation Name  6. Sex (M/F)  7. Age  8. EPIC No

EPIC prefix (e.g. DD/01/000/) appears on each data page header.

AC1 (Daman & Diu): English text PDFs — extracts cleanly.
AC2 (Dadra & Nagar Haveli): Scanned PDFs with an invisible-text OCR overlay
  in an unknown legacy Gujarati font (labelled "Helvetica" in the PDF but
  actually rendering Gujarati glyphs via unidentified ASCII→Gujarati mapping).
  82 of 125 parts are pure scans with no text layer at all; the 43 parts that
  have an OCR layer produce garbled ASCII (e.g. "iort lrt", "{rdgorld") because
  the font's ToUnicode CMap is missing and the encoding doesn't match any of
  the 94+ known Gujarati legacy fonts (LMG Arun, Harikrishna, Sulekh, EKLG,
  Gopika, Saral, etc.).  AC2 is skipped by default; pass --ac 2 to force
  extraction of the raw (garbled) text if needed for research.

Usage:
    python scripts/extract_dadra_nagar_haveli_daman_diu.py
    python scripts/extract_dadra_nagar_haveli_daman_diu.py --ac 1
    python scripts/extract_dadra_nagar_haveli_daman_diu.py --ac 1,2   # includes garbled AC2
    python scripts/extract_dadra_nagar_haveli_daman_diu.py --combined
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "dadra_nagar_haveli_daman_diu"
ROLL_YEAR = 2002
# AC2 (Dadra & Nagar Haveli) uses an undecodable legacy Gujarati font —
# scanned PDFs with invisible OCR text in an unknown encoding.  Skip by default.
SKIP_ACS = {2}
N_COLS = 8
NARROW_COLS = {4, 6, 7}
RELATION_CODES = {"F", "H", "M", "O", "W"}
ROW_TOL = 5.0
# AC001 data positions don't match column header positions —
# headers are right-shifted vs actual data.  These centres are
# measured from real data rows (see word x-positions in AC001 part 11).
# Measured from real data rows in AC001.  Serial at x≈55, house at x≈65,
# name at x≈142+, relation code at x≈327, rel-name at x≈339+, sex at x≈486,
# age at x≈504, EPIC at x≈546-550.
FALLBACK_CENTRES = {1: 55, 2: 80, 3: 200, 4: 327, 5: 380, 6: 486, 7: 504, 8: 550}
# Serial and house_no are only 10px apart, so auto-boundaries don't work.
FALLBACK_BOUNDARIES = {
    1: (0, 62), 2: (62, 135), 3: (135, 310), 4: (310, 335),
    5: (335, 470), 6: (470, 495), 7: (495, 525), 8: (525, 9999),
}
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "sex", "age", "epic_no",
    "roll_year",
]


# ── PDF extraction helpers ──────────────────────────────────────────────


def _group_into_rows(words):
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    current_row = [words_sorted[0]]
    current_top = words_sorted[0]["top"]
    for w in words_sorted[1:]:
        if w["top"] - current_top <= ROW_TOL:
            current_row.append(w)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [w]
            current_top = w["top"]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def _find_column_row(words, min_cols=6):
    if not words:
        return None
    parens = [w for w in words if re.fullmatch(r"\(\d+\)", w["text"])]
    if len(parens) >= min_cols:
        parens.sort(key=lambda w: w["top"])
        for i in range(len(parens)):
            group = [parens[i]]
            for j in range(i + 1, len(parens)):
                if abs(parens[j]["top"] - parens[i]["top"]) <= ROW_TOL:
                    group.append(parens[j])
            if len(group) >= min_cols:
                labels = sorted(group, key=lambda w: w["x0"])
                nums = [int(re.search(r"\d+", w["text"]).group()) for w in labels]
                if nums[0] == 1 and nums[1] == 2:
                    return {n: (w["x0"] + w["x1"]) / 2 for n, w in zip(nums, labels)}
    singles = [w for w in words if re.fullmatch(r"\d", w["text"])]
    if len(singles) >= min_cols:
        singles.sort(key=lambda w: w["top"])
        for i in range(len(singles)):
            group = [singles[i]]
            for j in range(i + 1, len(singles)):
                if abs(singles[j]["top"] - singles[i]["top"]) <= ROW_TOL:
                    group.append(singles[j])
            labels = sorted(group, key=lambda w: w["x0"])
            nums = [w["text"] for w in labels]
            if len(nums) >= min_cols and nums[0] == "1" and nums[1] == "2":
                return {int(w["text"]): (w["x0"] + w["x1"]) / 2 for w in labels}
    return None


def _make_boundaries(col_centres):
    cols = sorted(col_centres.keys())
    boundaries = {}
    for i, col in enumerate(cols):
        if i == 0:
            left = 0
        else:
            gap = col_centres[col] - col_centres[cols[i - 1]]
            if col in NARROW_COLS:
                left = col_centres[col] - gap * 0.2
            elif cols[i - 1] in NARROW_COLS:
                left = col_centres[cols[i - 1]] + gap * 0.2
            else:
                left = col_centres[cols[i - 1]] + gap * 0.5
        if i == len(cols) - 1:
            right = 9999
        else:
            gap = col_centres[cols[i + 1]] - col_centres[col]
            if col in NARROW_COLS:
                right = col_centres[col] + gap * 0.2
            elif cols[i + 1] in NARROW_COLS:
                right = col_centres[cols[i + 1]] - gap * 0.2
            else:
                right = col_centres[col] + gap * 0.5
        boundaries[col] = (left, right)
    return boundaries


def _assign_column(x_centre, col_centres, boundaries):
    for col, (left, right) in boundaries.items():
        if left <= x_centre < right:
            return col
    return min(col_centres, key=lambda c: abs(col_centres[c] - x_centre))


def _row_to_cells(row_words, col_centres, boundaries):
    buckets = {c: [] for c in col_centres}
    for w in row_words:
        cx = (w["x0"] + w["x1"]) / 2
        col = _assign_column(cx, col_centres, boundaries)
        buckets[col].append(w)
    cells = []
    for c in sorted(col_centres.keys()):
        parts = sorted(buckets.get(c, []), key=lambda w: w["x0"])
        cells.append(" ".join(w["text"] for w in parts).strip())
    return cells


def _find_col_row_top(words, col_centres):
    candidates = []
    for w in words:
        txt = w["text"]
        if re.fullmatch(r"\(?\d\)?", txt):
            cx = (w["x0"] + w["x1"]) / 2
            digit = int(re.search(r"\d", txt).group())
            if digit in col_centres and abs(cx - col_centres[digit]) < 15:
                candidates.append(w)
    if not candidates:
        return 0
    candidates.sort(key=lambda w: w["top"])
    col_row_top = 0
    group = [candidates[0]]
    for w in candidates[1:]:
        if w["top"] - group[0]["top"] <= ROW_TOL:
            group.append(w)
        else:
            if len(group) >= 3:
                col_row_top = max(col_row_top, max(g["top"] for g in group))
            group = [w]
    if len(group) >= 3:
        col_row_top = max(col_row_top, max(g["top"] for g in group))
    return col_row_top


def _find_epic_prefix(words):
    """Extract EPIC prefix like 'DD/01/000/' from the page header."""
    for w in words:
        if re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}/", w["text"]):
            return w["text"].rstrip("/")
        if re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}", w["text"]):
            return w["text"]
    return None


def _extract_page(page, fallback_centres=None, fallback_prefix=None):
    words = page.extract_words()
    if not words:
        return [], fallback_centres, fallback_prefix
    col_centres = FALLBACK_CENTRES
    if col_centres is None:
        return [], None, fallback_prefix
    epic_prefix = _find_epic_prefix(words) or fallback_prefix
    boundaries = FALLBACK_BOUNDARIES
    col_row_top = _find_col_row_top(words, col_centres)
    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    # Exclude EPIC prefix from data words
    data_words = [w for w in data_words
                  if not re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}/?", w["text"])]
    rows = _group_into_rows(data_words)
    records = []
    for rw in rows:
        cells = _row_to_cells(rw, col_centres, boundaries)
        serial = cells[0].strip() if cells else ""
        if serial and re.fullmatch(r"\d+", serial):
            cells = (cells + [""] * N_COLS)[:N_COLS]
            # Prepend EPIC prefix to 6-digit suffix
            epic = cells[7].strip()
            if epic and re.fullmatch(r"\d{6}", epic) and epic_prefix:
                cells[7] = epic_prefix + "/" + epic
            records.append(cells)
    return records, col_centres, epic_prefix


def _extract_pdf(pdf_bytes):
    all_rows = []
    col_centres = None
    epic_prefix = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, col_centres, epic_prefix = _extract_page(
                page, col_centres, epic_prefix
            )
            all_rows.extend(rows)
    return all_rows


def _extract_ac_zip(zip_path):
    all_rows = []
    manifest = {}
    with zipfile.ZipFile(zip_path) as zf:
        if "manifest.json" in zf.namelist():
            manifest = json.loads(zf.read("manifest.json"))
        part_files = sorted([n for n in zf.namelist() if n.endswith(".pdf")])
        for pf in part_files:
            m = re.search(r"(\d+)", os.path.basename(pf))
            if not m:
                continue
            part_no = int(m.group(1))
            rows = _extract_pdf(zf.read(pf))
            for cells in rows:
                all_rows.append([part_no] + cells)
    return all_rows, manifest


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


# ── Post-processing ─────────────────────────────────────────────────────


def _fix_row(row):
    """Fix column-bleeding issues.

    row layout: [part_no, serial, house_no, elector_name, relation,
                 relation_name, sex, age, epic_no]
    """
    HOUSE = 2
    NAME = 3
    REL = 4

    house = row[HOUSE].strip()
    name = row[NAME].strip()
    rel = row[REL].strip()

    # Fix 1a: purely alphabetic house_no is a misplaced elector name
    if house and all(p.isalpha() or p == "." for p in house.replace(" ", "")):
        name = (house + " " + name).strip() if name else house
        house = ""

    # Fix 1b: alpha words at end of house_no belong in elector_name
    house_parts = house.split()
    if len(house_parts) > 1:
        alpha_tail = []
        for i in range(len(house_parts) - 1, 0, -1):
            if all(c.isalpha() or c == "." for c in house_parts[i]):
                alpha_tail.insert(0, house_parts[i])
            else:
                break
        if alpha_tail:
            house = " ".join(house_parts[:len(house_parts) - len(alpha_tail)])
            name = " ".join(alpha_tail) + (" " + name if name else "")

    # Fix 2: relation code stuck at end of elector_name
    name_parts = name.split()
    if len(name_parts) >= 2 and name_parts[-1] in RELATION_CODES and not rel:
        rel = name_parts[-1]
        name = " ".join(name_parts[:-1])

    row[HOUSE] = house
    row[NAME] = name
    row[REL] = rel
    return row


# ── Main ─────────────────────────────────────────────────────────────────


# ── CSV post-processing ──────────────────────────────────────────────────

VALID_SEX = {"M", "F", ""}
VALID_RELATION = {"F", "H", "M", "O", ""}
_OCR_JUNK = set(".,;:'\"•■·-!?|/\\()[]{}<>` \t")


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    D&NH-specific fixes (OCR'd scanned PDFs, heavy noise):
    1. Sex column has EPIC numbers, ages, OCR garbage bleeding in.
       Try to extract F/M by stripping junk; else clear to empty.
    2. Relation column has OCR garbage. Try to extract F/H/M/O; else clear.
    """
    import tempfile, shutil

    fixes = {"sex_cleaned": 0, "rel_cleaned": 0, "total": 0}

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                fixes["total"] += 1
                sex = row["sex"].strip()
                rel = row["relation"].strip()

                if sex not in VALID_SEX:
                    # Try to find F or M in the noise
                    stripped = "".join(c for c in sex if c not in _OCR_JUNK and not c.isdigit())
                    if stripped in ("F", "M"):
                        row["sex"] = stripped
                    elif "F" in sex.split() or sex.endswith(" F"):
                        row["sex"] = "F"
                    elif "M" in sex.split() or sex.endswith(" M"):
                        row["sex"] = "M"
                    else:
                        row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                if rel not in VALID_RELATION:
                    stripped = "".join(c for c in rel if c not in _OCR_JUNK and not c.isdigit())
                    if stripped in ("F", "H", "M", "O"):
                        row["relation"] = stripped
                    else:
                        row["relation"] = ""
                    fixes["rel_cleaned"] += 1

                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['sex_cleaned']} sex values fixed")
    print(f"  {fixes['rel_cleaned']} relation values fixed")


def main():
    ap = argparse.ArgumentParser(description="Extract D&NH SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/dadra_nagar_haveli_daman_diu")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to fix column bleeding")
    args = ap.parse_args()

    if args.postprocess:
        _postprocess(args.postprocess)
        return

    raw_dir = os.path.join("data", "raw", STATE_ID)
    os.makedirs(args.out_dir, exist_ok=True)

    meta = _load_meta()
    ac_map = {ac["ac_no"]: ac for ac in meta}

    zip_files = sorted([f for f in os.listdir(raw_dir) if f.endswith(".zip")])
    if args.ac:
        wanted = set(int(x) for x in args.ac.split(","))
        zip_files = [f for f in zip_files if int(re.search(r"\d+", f).group()) in wanted]
    else:
        # Skip ACs with undecodable legacy fonts unless explicitly requested
        skipped = []
        kept = []
        for f in zip_files:
            ac = int(re.search(r"\d+", f).group())
            if ac in SKIP_ACS:
                skipped.append(f)
            else:
                kept.append(f)
        if skipped:
            print(f"{STATE_ID}: skipping {len(skipped)} AC(s) with undecodable "
                  f"Gujarati font: {', '.join(skipped)}")
            print("  (pass --ac 2 to force extraction of garbled text)")
        zip_files = kept
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
        rows, _ = _extract_ac_zip(zip_path)
        rows = [_fix_row(r) for r in rows]
        full_rows = [[STATE_ID, district, ac_no, ac_name] + r + [ROLL_YEAR] for r in rows]

        if args.combined:
            all_rows.extend(full_rows)
        else:
            csv_path = os.path.join(args.out_dir, f"AC{ac_no:03d}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADERS)
                w.writerows(full_rows)

        print(f"{len(rows)} rows")

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
