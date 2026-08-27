"""
Extract NCT Delhi old SIR voter roll PDFs to CSV.

PDF layout: 8 columns
  1. Serial No  2. House No  3. Elector Name  4. Relation (F/H/M/O)
  5. Relation Name  6. Sex (M/F)  7. Age  8. EPIC No

EPIC numbers in the PDF are 6-digit suffixes; the prefix (e.g. DL/01/001)
appears once per page above the data rows and must be prepended.

Usage:
    python scripts/extract_delhi.py                    # all downloaded ACs
    python scripts/extract_delhi.py --ac 1,2,3         # specific ACs
    python scripts/extract_delhi.py --limit 5          # first 5 ACs
    python scripts/extract_delhi.py --combined         # single CSV for state
    python scripts/extract_delhi.py --postprocess path/to/delhi_combined.csv
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "delhi"
ROLL_YEAR = 2002
N_COLS = 8
NARROW_COLS = {4, 6, 7}
RELATION_CODES = {"F", "H", "M", "O", "W"}
ROW_TOL = 5.0
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
    """Find y of the column-number header row to skip.

    Requires 3+ column markers at the same y so a lone data '1' isn't
    mistaken for a header.
    """
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
    """Extract EPIC prefix like 'DL/01/001' from the page header area."""
    for w in words:
        if re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}", w["text"]):
            return w["text"]
    return None


def _extract_page(page, fallback_centres=None, fallback_prefix=None):
    """Returns (records, col_centres, epic_prefix)."""
    words = page.extract_words()
    if not words:
        return [], fallback_centres, fallback_prefix
    col_centres = _find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None, fallback_prefix
    epic_prefix = _find_epic_prefix(words) or fallback_prefix
    boundaries = _make_boundaries(col_centres)
    col_row_top = _find_col_row_top(words, col_centres)
    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    # Exclude the EPIC prefix word from data
    data_words = [w for w in data_words
                  if not re.fullmatch(r"[A-Z]{2}/\d{2}/\d{3}", w["text"])]
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
    """Fix column-bleeding issues in extracted rows.

    row layout: [part_no, serial, house_no, elector_name, relation,
                 relation_name, sex, age, epic_no]

    Fixes:
    1. Alpha words bleeding from elector_name into house_no
    2. Relation code (F/H/M/O) stuck at end of elector_name
    """
    HOUSE = 2
    NAME = 3
    REL = 4

    house = row[HOUSE].strip()
    name = row[NAME].strip()
    rel = row[REL].strip()

    # Fix 1a: if house_no is purely alphabetic, it's a misplaced elector name
    # (real house numbers always contain digits like A-1, B-2, E-2(1))
    if house and all(p.isalpha() or p == "." for p in house.replace(" ", "")):
        name = (house + " " + name).strip() if name else house
        house = ""

    # Fix 1b: alpha words at end of house_no belong in elector_name
    house_parts = house.split()
    if len(house_parts) > 1:
        alpha_tail = []
        for i in range(len(house_parts) - 1, 0, -1):
            if house_parts[i].isalpha():
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


# ── CSV post-processing ──────────────────────────────────────────────────

VALID_SEX = {"M", "F", ""}
VALID_RELATION = {"F", "H", "M", "O", "W", "f", "h", ""}


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    Delhi-specific fixes:
    1. Sex column has name-initial prefix before F/M (e.g. "K M", "B F",
       "JA M") — extract last token as sex, prepend rest to elector_name.
    2. Sex column has age prefix (e.g. "27 F") — extract F, ignore the
       number (age column already has it).
    """
    import tempfile, shutil

    fixes = {"sex_cleaned": 0, "total": 0}

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
                if sex not in VALID_SEX:
                    parts = sex.split()
                    last = parts[-1] if parts else ""
                    if last in ("M", "F"):
                        prefix = " ".join(parts[:-1])
                        # Prepend non-numeric prefix to elector_name
                        if prefix and not prefix.replace(" ", "").isdigit():
                            row["elector_name"] = (prefix + " " + row["elector_name"]).strip()
                        row["sex"] = last
                        fixes["sex_cleaned"] += 1
                    else:
                        row["sex"] = ""
                        fixes["sex_cleaned"] += 1
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


# ── Main ─────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description="Extract NCT Delhi SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/delhi")
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
