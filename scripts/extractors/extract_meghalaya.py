"""
Extract Meghalaya old SIR (2005) voter roll PDFs to CSV.

PDF uses Scanned+OCR GlyphLessFont. Text extracted as-is using word-position
column detection. Column number markers (1)(2)...(8) or 1 2...8 used for alignment.

Usage:
    python scripts/extractors/extract_meghalaya.py                    # all downloaded ACs
    python scripts/extractors/extract_meghalaya.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_meghalaya.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_meghalaya.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "meghalaya"
ROLL_YEAR = 2005
N_COLS = 8
NARROW_COLS = {4, 6, 7}
ROW_TOL = 5.0
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "sex", "age", "epic_no",
    "roll_year",
]

# Column indices inside an 8-cell row (0-based)
_SERIAL = 0
_HOUSE = 1
_NAME = 2
_REL = 3


# -- PDF extraction helpers ---------------------------------------------------


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


def _fix_row(row):
    """Post-process an 8-cell row to fix common mis-assignments.

    - Purely alphabetic house_no moved to elector_name
    - Trailing alpha words in house_no moved to elector_name
    - Relation code (F/H/M/O/W) stuck at end of elector_name moved to relation
    """
    house = row[_HOUSE].strip()
    name = row[_NAME].strip()
    rel = row[_REL].strip()

    # Purely alphabetic house_no -> belongs in elector_name
    if house and re.fullmatch(r"[A-Za-z ]+", house):
        name = f"{house} {name}".strip()
        house = ""

    # Trailing alpha words in house_no -> move to elector_name
    if house:
        m = re.match(r"^(.*?\d[\d/\-]*)(\s+[A-Za-z][\w ]*?)$", house)
        if m:
            house = m.group(1).strip()
            name = f"{m.group(2).strip()} {name}".strip()

    # Relation code stuck at end of elector_name
    if not rel:
        m = re.match(r"^(.*?)\s+([FHMOW])$", name)
        if m:
            name = m.group(1).strip()
            rel = m.group(2)

    row[_HOUSE] = house
    row[_NAME] = name
    row[_REL] = rel
    return row


def _extract_page(page, fallback_centres=None):
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    col_centres = _find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None
    boundaries = _make_boundaries(col_centres)
    col_row_top = _find_col_row_top(words, col_centres)
    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    rows = _group_into_rows(data_words)
    records = []
    for rw in rows:
        cells = _row_to_cells(rw, col_centres, boundaries)
        serial = cells[0].strip() if cells else ""
        if serial and re.fullmatch(r"\d+", serial):
            cells = (cells + [""] * N_COLS)[:N_COLS]
            _fix_row(cells)
            records.append(cells)
    return records, col_centres


def _extract_pdf(pdf_bytes):
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, col_centres = _extract_page(page, col_centres)
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


# -- Main ----------------------------------------------------------------------


# ── CSV post-processing ──────────────────────────────────────────────────

VALID_SEX = {"M", "F", ""}
VALID_RELATION = {"F", "H", "M", "O", ""}


def _extract_sex(val):
    """Try to extract F or M from OCR-noisy sex values.

    Common Meghalaya patterns:
    - "M 18", "F 20" → F/M with age appended
    - "M18", "F22" → concatenated
    - "Fo", "Foo", "FB", "FE", "FO" → OCR misread of F
    - "Mo", "Moa", "Moat", "Mom", "MB" → OCR misread of M
    - "18", "30" → pure age (sex missing)
    - "VI", "IV", "a", "r" → OCR garbage
    """
    if not val:
        return ""
    first = val[0]
    if first == "F" and (len(val) == 1 or not val[1:].strip().isalpha()
                          or val[1:] in ("o", "oo", "oot", "B", "E", "O")):
        return "F"
    if first == "M" and (len(val) == 1 or not val[1:].strip().isalpha()
                          or val[1:] in ("o", "oa", "oat", "om", "oo", "B", "8")):
        return "M"
    # "M =" pattern
    if val.startswith("M ") or val.startswith("M="):
        return "M"
    if val.startswith("F ") or val.startswith("F="):
        return "F"
    return ""


_EPIC_RE = re.compile(
    r"(?:MG/\d{2}/\d{3}/\d{5,})"       # MG/01/004/000294
    r"|(?:[A-Z]{2,4}\d{5,})"            # GTD0181503, DWNO0142059, BZX0178970
)

_DIGIT_EPIC_RE = re.compile(
    r"^(\d{1,3})\s+"                    # leading age digits + space
    r"(" + _EPIC_RE.pattern + r".*?)$"  # EPIC (possibly OCR-damaged tail)
)


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    Meghalaya-specific fixes (OCR'd scanned PDFs, heavy noise):

    1. **EPIC-in-age (digit+EPIC)**: age like "51 MG/01/004/000409" — split
       into age=51 and epic_no=MG/01/004/000409 (only when epic_no is empty).

    2. **EPIC-in-age (pure EPIC, full column shift)**: age is a bare EPIC
       with no leading digits.  The entire right half of the row is shifted
       one column right: relation_name holds sex (M/F), sex is empty, age
       holds EPIC, epic_no is empty.  Fix: move relation_name→sex,
       age→epic_no, clear age.

    3. **Sex column noise**: OCR misreads like "Fo", "Moa", "M 18", "F20",
       pure ages ("18"), or garbage ("VI", "a").  Extract F/M where
       possible; else clear.

    4. **elector_name = "F 55" / "M 30"**: sex+age landed in name column
       (rest of row usually empty).  Move sex and age out of name; clear
       name since actual name is lost.

    5. **Relation column noise**: same OCR junk treatment — keep only
       F/H/M/O/W, clear the rest.

    6. **EPIC in relation_name (no elector_name)**: relation_name sometimes
       holds an EPIC when elector_name is empty — move to epic_no.

    7. **Drop empty-name junk rows**: serial_no=0 with no elector_name,
       relation_name, sex, or age is OCR debris — drop entirely.
    """
    import tempfile, shutil

    fixes = {
        "total": 0, "kept": 0, "dropped": 0,
        "epic_from_age_split": 0, "epic_from_age_shift": 0,
        "sex_from_shift": 0, "sex_cleaned": 0, "sex_from_name": 0,
        "age_from_name": 0, "rel_cleaned": 0, "name_cleared": 0,
        "epic_from_rname": 0, "epic_from_name": 0,
        "sex_age_from_rname": 0, "header_dropped": 0,
    }

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
                age = row["age"].strip()
                epic = row["epic_no"].strip()
                name = row["elector_name"].strip()
                rname = row["relation_name"].strip()
                sn = row["serial_no"].strip()

                # --- Fix 1: digit+EPIC in age ---------------------------
                dm = _DIGIT_EPIC_RE.match(age)
                if dm and not epic:
                    row["age"] = dm.group(1)
                    row["epic_no"] = re.sub(r"\s+", "", dm.group(2))  # OCR spaces
                    age = row["age"]
                    epic = row["epic_no"]
                    fixes["epic_from_age_split"] += 1

                # --- Fix 2: pure EPIC in age (full column shift) ---------
                elif _EPIC_RE.search(age) and not epic:
                    row["epic_no"] = re.sub(r"\s+", "", age)
                    row["age"] = ""
                    epic = row["epic_no"]
                    age = ""
                    fixes["epic_from_age_shift"] += 1
                    # relation_name likely holds sex (M/F) in shifted rows
                    if not sex and rname:
                        rname_clean = rname.strip().rstrip("'").upper()
                        if rname_clean in ("M", "F"):
                            row["sex"] = rname_clean
                            row["relation_name"] = ""
                            sex = rname_clean
                            rname = ""
                            fixes["sex_from_shift"] += 1

                # --- Fix 3a: EPIC as elector_name ----------------------
                if name and not epic and _EPIC_RE.fullmatch(name):
                    row["epic_no"] = name
                    row["elector_name"] = ""
                    epic = name
                    name = ""
                    fixes["epic_from_name"] += 1

                # --- Fix 3b: header junk in elector_name ---------------
                if re.search(r"Original.*Mother.*Roll|Intensive.*Revision"
                             r"|Male.*Female|Page\s+No", name, re.I):
                    fixes["header_dropped"] += 1
                    fixes["dropped"] += 1
                    continue

                # --- Fix 4: elector_name = "F 55" / "M 30" --------------
                nm = re.fullmatch(r"([FM])\s+(\d{1,3})", name)
                if nm:
                    if not sex:
                        row["sex"] = nm.group(1)
                        sex = nm.group(1)
                        fixes["sex_from_name"] += 1
                    if not age:
                        row["age"] = nm.group(2)
                        age = nm.group(2)
                        fixes["age_from_name"] += 1
                    row["elector_name"] = ""
                    name = ""
                    fixes["name_cleared"] += 1

                # --- Fix 3: sex column noise -----------------------------
                if sex not in VALID_SEX:
                    row["sex"] = _extract_sex(sex)
                    fixes["sex_cleaned"] += 1

                # --- Fix 5: relation column noise ------------------------
                if rel not in VALID_RELATION:
                    stripped = rel.strip()
                    parts = stripped.split()
                    if parts and parts[0] in ("F", "H", "M", "O", "W"):
                        row["relation"] = parts[0]
                    elif parts and parts[-1] in ("F", "H", "M", "O", "W"):
                        row["relation"] = parts[-1]
                    else:
                        row["relation"] = ""
                    fixes["rel_cleaned"] += 1

                # --- Fix 5b: sex+age in relation_name ("M 39", "F 20") ---
                rname = row["relation_name"].strip()
                sex = row["sex"].strip()
                age = row["age"].strip()
                rm = re.fullmatch(r"([FM])\s+(\d{1,3})", rname)
                if rm:
                    if not sex:
                        row["sex"] = rm.group(1)
                        sex = rm.group(1)
                    if not age:
                        row["age"] = rm.group(2)
                        age = rm.group(2)
                    row["relation_name"] = ""
                    rname = ""
                    fixes["sex_age_from_rname"] += 1

                # --- Fix 6: EPIC in relation_name when name is empty -----
                rname = row["relation_name"].strip()
                if not name and not epic and rname and _EPIC_RE.fullmatch(rname):
                    row["epic_no"] = rname
                    row["relation_name"] = ""
                    fixes["epic_from_rname"] += 1

                # --- Fix 7: drop empty junk rows -------------------------
                name = row["elector_name"].strip()
                rname = row["relation_name"].strip()
                sex = row["sex"].strip()
                age = row["age"].strip()
                sn = row["serial_no"].strip()
                if not name and not rname and not sex and not age:
                    if sn == "0" or not sn:
                        fixes["dropped"] += 1
                        continue

                # --- Clean non-numeric age remnants ----------------------
                age = row["age"].strip()
                if age and not re.fullmatch(r"\d{1,3}", age):
                    # Try to extract leading digits
                    m = re.match(r"^(\d{1,3})\b", age)
                    row["age"] = m.group(1) if m else ""

                fixes["kept"] += 1
                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed, {fixes['kept']:,} kept, "
          f"{fixes['dropped']:,} dropped")
    print(f"  {fixes['epic_from_age_split']:,} EPIC split from age (digit+EPIC)")
    print(f"  {fixes['epic_from_age_shift']:,} EPIC moved from age (pure EPIC, column shift)")
    print(f"  {fixes['sex_from_shift']:,} sex recovered from shifted relation_name")
    print(f"  {fixes['sex_from_name']:,} sex recovered from elector_name (F 55 pattern)")
    print(f"  {fixes['sex_cleaned']:,} sex values cleaned (OCR noise)")
    print(f"  {fixes['age_from_name']:,} age recovered from elector_name")
    print(f"  {fixes['name_cleared']:,} elector_name cleared (was sex+age)")
    print(f"  {fixes['rel_cleaned']:,} relation values cleaned")
    print(f"  {fixes['sex_age_from_rname']:,} sex+age recovered from relation_name")
    print(f"  {fixes['epic_from_name']:,} EPIC moved from elector_name")
    print(f"  {fixes['epic_from_rname']:,} EPIC moved from relation_name")
    print(f"  {fixes['header_dropped']:,} header junk rows dropped")


def main():
    ap = argparse.ArgumentParser(description="Extract Meghalaya SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/meghalaya")
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
