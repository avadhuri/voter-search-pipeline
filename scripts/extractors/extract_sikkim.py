"""
Extract Sikkim old SIR voter roll PDFs to CSV.

Sikkim PDFs are scanned/OCR'd — expect noise (misread chars, merged
columns). This extractor handles the structural issues; OCR typos in
names/EPIC numbers are inherent to the source.

PDF layout: 8 columns (sometimes only 7 detected on cover pages)
  1. Serial No  2. House No  3. Elector Name  4. Relation (F/H/M/O)
  5. Relation Name  6. Sex (M/F)  7. Age  8. EPIC No

Usage:
    python scripts/extract_sikkim.py                    # all downloaded ACs
    python scripts/extract_sikkim.py --ac 1,2,3         # specific ACs
    python scripts/extract_sikkim.py --limit 5          # first 5 ACs
    python scripts/extract_sikkim.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "sikkim"
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


def _extract_page(page, fallback_centres=None):
    words = page.extract_words()
    if not words:
        return [], fallback_centres
    col_centres = _find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None
    # Sikkim page 1 sometimes only detects 7 cols (no col 8 for EPIC).
    # Prefer 8-col centres when available; skip pages with fewer than 8
    # until we get a proper 8-col fallback.
    if len(col_centres) < N_COLS and fallback_centres and len(fallback_centres) >= N_COLS:
        col_centres = fallback_centres
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
            records.append(cells)
    return records, col_centres


def _extract_pdf(pdf_bytes):
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, new_centres = _extract_page(page, col_centres)
            # Only update fallback centres if we found 8 columns
            if new_centres and len(new_centres) >= N_COLS:
                col_centres = new_centres
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
    """Fix column-bleeding and OCR artifacts.

    row layout: [part_no, serial, house_no, elector_name, relation,
                 relation_name, sex, age, epic_no]
    """
    HOUSE = 2
    NAME = 3
    REL = 4
    AGE = 7
    EPIC = 8

    house = row[HOUSE].strip()
    name = row[NAME].strip()
    rel = row[REL].strip()
    age = row[AGE].strip()
    epic = row[EPIC].strip()

    # Fix 1a: purely alphabetic house_no is a misplaced elector name
    if house and all(p.isalpha() or p == "." for p in house.replace(" ", "")):
        name = (house + " " + name).strip() if name else house
        house = ""

    # Fix 1b: alpha/initial words at end of house_no belong in elector_name
    house_parts = house.split()
    if len(house_parts) > 1:
        alpha_tail = []
        for i in range(len(house_parts) - 1, 0, -1):
            if all(c.isalpha() or c == "." or c == "'" for c in house_parts[i]):
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

    # Fix 3: age and EPIC merged in age column (e.g. "53 000003")
    age_parts = age.split()
    if len(age_parts) == 2 and not epic:
        age = age_parts[0]
        epic = age_parts[1]

    # Fix 4: "N" in EPIC means no card — normalize to empty
    if epic == "N" or epic == ".N":
        epic = ""

    row[HOUSE] = house
    row[NAME] = name
    row[REL] = rel
    row[AGE] = age
    row[EPIC] = epic
    return row


# ── Main ─────────────────────────────────────────────────────────────────


# ── CSV post-processing ──────────────────────────────────────────────────

VALID_SEX = {"M", "F", ""}
VALID_RELATION = {"F", "H", "M", "O", ""}
_OCR_JUNK = set(".,;:'\"\u2019\u2018\u201c\u201d\u2022\u25a0\u00b7\u00ad-!?|/\\()[]{}<>` \t")

# OCR digit substitution map for age cleanup.
# Common misreads in Sikkim scanned PDFs:
#   ! → 1, ) → 9, S → 5, I → 1, O → 0, s → 5, o → 0, i → 1
_OCR_DIGIT_MAP = str.maketrans("!)SIOsio", "19510510")


def _clean_fhmo(val, valid=("F", "H", "M", "O")):
    """Try to extract a valid F/H/M/O code from OCR-noisy text.

    Strips punctuation/OCR artifacts and looks for a single valid code.
    Returns (cleaned_code, prefix_to_prepend_to_name) or (None, None).
    """
    # Strip OCR junk chars and check what's left
    stripped = "".join(c for c in val if c not in _OCR_JUNK)
    if stripped in valid:
        return stripped, ""
    # Space-separated: "H NARZANG" → first token might be the code
    parts = val.split()
    if parts and parts[0] in valid:
        return parts[0], " ".join(parts[1:])
    # Last token: "BDR. F" → last token might be the code
    if parts and parts[-1] in valid:
        return parts[-1], " ".join(parts[:-1])
    # Last token with junk stripped: "F-" "M." "F'" etc.
    if parts:
        last_stripped = "".join(c for c in parts[-1] if c not in _OCR_JUNK)
        if last_stripped in valid:
            return last_stripped, " ".join(parts[:-1])
    return None, None


def _clean_age(val):
    """Clean OCR artifacts from an age value.

    Handles: leading/trailing junk ("20.", ".35", "34 .", "' 29", "■ 22"),
    OCR digit substitution (2! → 21, S3 → 53, SO → 50, IS → 15),
    and embedded junk (1.8 → 18).

    Returns cleaned age string (digits only) or "" if unrecoverable.
    """
    if not val:
        return ""
    # Strip all OCR junk and whitespace, keep alphanumeric
    stripped = "".join(c for c in val if c not in _OCR_JUNK and not c.isspace())
    if not stripped:
        return ""
    # Apply OCR digit substitution
    translated = stripped.translate(_OCR_DIGIT_MAP)
    # After translation, should be all digits
    if translated.isdigit():
        age_int = int(translated)
        if 1 <= age_int <= 150:
            return str(age_int)
    # Try extracting just the digits from the original
    digits = re.sub(r"[^0-9]", "", val)
    if digits:
        age_int = int(digits)
        if 10 <= age_int <= 150:
            return str(age_int)
    return ""


def _extract_sex_from_relation_name(relation_name):
    """Extract trailing sex code (F/M) from relation_name.

    Sikkim column-shift rows have sex appended to relation_name, e.g.
    "GURUNG F", "BHUTIA M", "LIMBU F-".
    Returns (cleaned_relation_name, sex) or (relation_name, "").
    """
    if not relation_name:
        return relation_name, ""
    parts = relation_name.strip().split()
    if not parts:
        return relation_name, ""
    last = parts[-1]
    # Strip junk from last token
    last_clean = "".join(c for c in last if c not in _OCR_JUNK)
    if last_clean in ("F", "M"):
        return " ".join(parts[:-1]), last_clean
    return relation_name.strip(), ""


def _postprocess(csv_path):
    """Fix column-bleeding and OCR issues in an already-extracted CSV.

    Sikkim-specific fixes (OCR'd scanned PDFs):

    Column-shift repairs (run first, before field-level fixes):
    A. age='M'/'F', sex='', epic='age EPIC' or 'age N':
       Sex leaked into age, real age+epic merged in epic column.
       → sex=age, split epic into age+epic.
    B. age='N', sex='', epic='':
       EPIC 'N' (no card) shifted into age; sex appended to relation_name.
       → age='', epic='', extract sex from relation_name tail.
    C. age is a 6-digit EPIC number, sex='', epic='':
       EPIC shifted into age; sex appended to relation_name tail.
       → epic=age, age='', extract sex from relation_name tail.

    Field-level fixes (run after shift repair):
    1. Age OCR cleanup: strip junk chars, apply digit substitution
       (! → 1, ) → 9, S → 5, I → 1, O → 0).
    2. Sex column: strip OCR punctuation around F/M.
    3. Relation column: strip OCR punctuation, extract name bleed.
    """
    import tempfile, shutil

    fixes = {
        "total": 0, "sex_cleaned": 0, "rel_cleaned": 0,
        "age_cleaned": 0, "shift_sex_in_age": 0,
        "shift_n_in_age": 0, "shift_epic_in_age": 0,
        "sex_from_relname": 0,
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
                age = row["age"].strip()
                sex = row["sex"].strip()
                epic = row["epic_no"].strip()
                rel = row["relation"].strip()
                relname = row["relation_name"].strip()

                # ── Column-shift repair A: sex (M/F) leaked into age ──
                # age='M'/'F', sex empty, epic has "real_age real_epic"
                if age in ("M", "F") and sex == "":
                    row["sex"] = age
                    fixes["shift_sex_in_age"] += 1
                    # Split epic: "50 003279" → age=50, epic=003279
                    # "35 N" → age=35, epic="" (N = no card)
                    epic_parts = epic.split()
                    if len(epic_parts) >= 2:
                        row["age"] = epic_parts[0]
                        ep = " ".join(epic_parts[1:])
                        row["epic_no"] = "" if ep == "N" else ep
                    elif epic and epic.isdigit() and len(epic) <= 3:
                        row["age"] = epic
                        row["epic_no"] = ""
                    else:
                        row["age"] = ""
                    age = row["age"].strip()
                    sex = row["sex"].strip()
                    epic = row["epic_no"].strip()

                # ── Column-shift repair B: 'N' (no EPIC) leaked into age ──
                # age='N' (or N with junk: "N'", "N\u2019"), sex empty
                # — sex is at tail of relation_name
                elif (age.rstrip("'.;\u2019\u2018-") == "N"
                      and not any(c.isdigit() for c in age)
                      and sex == ""):
                    row["age"] = ""
                    row["epic_no"] = ""  # N means no card
                    fixes["shift_n_in_age"] += 1
                    # Extract sex from relation_name tail
                    new_relname, extracted_sex = _extract_sex_from_relation_name(relname)
                    if extracted_sex:
                        row["sex"] = extracted_sex
                        row["relation_name"] = new_relname
                        fixes["sex_from_relname"] += 1
                    age = ""
                    sex = row["sex"].strip()
                    epic = ""

                # ── Column-shift repair C: EPIC number leaked into age ──
                # age is 6+ digit EPIC, sex empty, epic empty
                elif (age and len(age) >= 6 and age.isdigit()
                      and sex == "" and epic == ""):
                    row["epic_no"] = age
                    row["age"] = ""
                    fixes["shift_epic_in_age"] += 1
                    # Extract sex from relation_name tail
                    new_relname, extracted_sex = _extract_sex_from_relation_name(relname)
                    if extracted_sex:
                        row["sex"] = extracted_sex
                        row["relation_name"] = new_relname
                        fixes["sex_from_relname"] += 1
                    age = ""
                    sex = row["sex"].strip()
                    epic = row["epic_no"].strip()

                # ── Age OCR cleanup ──
                age = row["age"].strip()
                if age and not age.isdigit():
                    cleaned = _clean_age(age)
                    if cleaned != age:
                        row["age"] = cleaned
                        fixes["age_cleaned"] += 1
                    age = row["age"].strip()
                # Validate numeric age: voters must be 18+, cap at 120
                if age and age.isdigit():
                    age_int = int(age)
                    if age_int < 10 or age_int > 120:
                        row["age"] = ""
                        fixes["age_cleaned"] += 1

                # ── Fix sex ──
                sex = row["sex"].strip()
                if sex not in VALID_SEX:
                    code, _ = _clean_fhmo(sex, valid=("F", "M"))
                    if code:
                        row["sex"] = code
                    else:
                        row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                # ── Fix relation ──
                rel = row["relation"].strip()
                if rel not in VALID_RELATION:
                    code, prefix = _clean_fhmo(rel)
                    if code:
                        if prefix and not all(c in _OCR_JUNK for c in prefix):
                            row["elector_name"] = (row["elector_name"].strip() + " " + prefix).strip()
                        row["relation"] = code
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
    print(f"  {fixes['shift_sex_in_age']} column-shift: sex in age (M/F → sex, epic split)")
    print(f"  {fixes['shift_n_in_age']} column-shift: N in age (no-EPIC marker)")
    print(f"  {fixes['shift_epic_in_age']} column-shift: EPIC number in age")
    print(f"  {fixes['sex_from_relname']} sex extracted from relation_name tail")
    print(f"  {fixes['age_cleaned']} age values OCR-cleaned")
    print(f"  {fixes['sex_cleaned']} sex values cleaned")
    print(f"  {fixes['rel_cleaned']} relation values cleaned")


def main():
    ap = argparse.ArgumentParser(description="Extract Sikkim SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/sikkim")
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
