"""
Extract Tripura old SIR (2005) voter roll PDFs to CSV.

PDF uses Bengali Unicode script. Text extracted as-is using word-position
column detection. Column number markers (1)(2)...(8) or 1 2...8 used
for alignment.

Usage:
    python scripts/extractors/extract_tripura.py                    # all downloaded ACs
    python scripts/extractors/extract_tripura.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_tripura.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_tripura.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "tripura"
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

# Column indices inside a row (0-based, after serial_no is cells[0])
_HOUSE = 1
_NAME = 2
_REL = 3


# -- PDF extraction helpers -----------------------------------------------


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
    """Post-process a row to fix common column-assignment errors.

    Fixes applied:
    - Purely alphabetic house_no moved to elector_name
    - Trailing alpha words in house_no moved to elector_name
    - Relation code (F/H/M/O/W) stuck at end of elector_name moved to relation
    """
    house = row[_HOUSE]
    name = row[_NAME]
    rel = row[_REL]

    # Purely alphabetic house_no -> actually elector_name
    if house and house.replace(" ", "").isalpha():
        name = f"{house} {name}".strip() if name else house
        house = ""

    # Trailing alpha words in house_no -> elector_name
    if house:
        parts = house.split()
        digits = []
        alpha_tail = []
        found_alpha = False
        for p in parts:
            if not found_alpha and re.search(r"\d", p):
                digits.append(p)
            else:
                found_alpha = True
                alpha_tail.append(p)
        if alpha_tail:
            house = " ".join(digits)
            name = " ".join(alpha_tail + ([name] if name else []))

    # Relation code stuck at end of elector_name
    if name and not rel:
        parts = name.rsplit(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[FHMOW]", parts[1]):
            name = parts[0]
            rel = parts[1]

    row[_HOUSE] = house
    row[_NAME] = name
    row[_REL] = rel


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


# -- Postprocess ------------------------------------------------------------

# Standard output schema
OUT_HEADERS = [
    "ac_number", "part_number", "serial_number", "elector_name",
    "relation_name", "relation_type", "sex", "age", "locality",
    "roll_year",
]

# Known Bengali relation prefixes (order matters: longer first)
_REL_PREFIXES = [
    ("স্ব্য", "স্ব্য"),  # husband (variant)
    ("স্বঁ", "স্বঁ"),    # husband (variant)
    ("স্বা", "স্বা"),    # husband
    ("ম্য", "ম্য"),     # mother (variant)
    ("মঁ", "মঁ"),       # mother (variant)
    ("মা", "মা"),       # mother
    ("িপ", "িপ"),       # father
    ("অ", "অ"),         # other
]

# Valid sex values in the source
_SEX_MAP = {"পুং": "M", "স্ত্রী": "F"}
_VALID_SEX_BN = set(_SEX_MAP.keys())

# Valid relation codes in source
_VALID_REL = {"িপ", "স্ব্য", "স্বঁ", "স্বা", "ম্য", "মঁ", "মা", "অ"}


def _postprocess(csv_path):
    """Post-process Tripura CSV: normalise schema, sex, and relation fields.

    Fixes applied:
    1. Rename columns to standard schema, drop house_no/epic_no/roll_year/
       state/district/ac_name.
    2. Convert Bengali sex (পুং→M, স্ত্রী→F).
    3. Fix column-shifted rows where sex has a numeric value (age leaked
       into sex, sex text leaked into relation_name).
    4. For rows with empty relation: extract Bengali relation prefix from
       relation_name and move to relation_type.
    5. For rows with relation code stuck at end of elector_name: extract
       it and move to relation_type.

    Re-runnable: auto-detects whether input uses old schema (ac_no,
    relation) or new schema (ac_number, relation_type).
    """
    import tempfile
    import shutil

    stats = {
        "total": 0,
        "sex_mapped": 0,
        "col_shift_fixed": 0,
        "rel_extracted_from_relname": 0,
        "rel_extracted_from_name": 0,
        "rel_already_set": 0,
    }

    # Detect schema by peeking at header
    with open(csv_path, encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    old_schema = "ac_no" in header

    # Column name mapping for field access
    if old_schema:
        F_AC = "ac_no"
        F_PART = "part_no"
        F_SN = "serial_no"
        F_REL = "relation"
    else:
        F_AC = "ac_number"
        F_PART = "part_number"
        F_SN = "serial_number"
        F_REL = "relation_type"

    tmp = tempfile.NamedTemporaryFile(
        mode="w", newline="", encoding="utf-8",
        dir=os.path.dirname(csv_path) or ".",
        suffix=".csv", delete=False,
    )
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=OUT_HEADERS)
            writer.writeheader()

            for row in reader:
                stats["total"] += 1

                rel = row[F_REL].strip()
                rel_name = row["relation_name"].strip()
                sex = row["sex"].strip()
                age = row["age"].strip()
                name = row["elector_name"].strip()

                # --- Fix column-shifted rows ---
                # When sex has a numeric value, it means:
                #   relation_name absorbed the sex text at the end,
                #   sex actually has the age, age is empty.
                if sex and sex.isdigit() and not age:
                    age = sex
                    sex = ""
                    # Try to extract Bengali sex from end of relation_name
                    for bn_sex in ("পুং", "স্ত্রী"):
                        if rel_name.endswith(bn_sex):
                            sex = _SEX_MAP[bn_sex]
                            rel_name = rel_name[:-len(bn_sex)].strip()
                            break
                    stats["col_shift_fixed"] += 1

                # --- Normalise sex ---
                if sex in _SEX_MAP:
                    sex = _SEX_MAP[sex]
                    stats["sex_mapped"] += 1
                elif sex not in ("M", "F", ""):
                    sex = ""

                # --- Extract relation prefix from relation_name ---
                if rel and rel in _VALID_REL:
                    stats["rel_already_set"] += 1
                elif not rel and rel_name:
                    for prefix, code in _REL_PREFIXES:
                        if rel_name.startswith(prefix):
                            rest = rel_name[len(prefix):].strip()
                            if rest:  # only if there's actual name left
                                rel = code
                                rel_name = rest
                                stats["rel_extracted_from_relname"] += 1
                                break

                # --- Extract relation code from end of elector_name ---
                if not rel and name:
                    for suffix, code in _REL_PREFIXES:
                        if name.endswith(" " + suffix):
                            name = name[: -(len(suffix) + 1)].strip()
                            rel = code
                            stats["rel_extracted_from_name"] += 1
                            break

                out = {
                    "ac_number": row[F_AC].strip(),
                    "part_number": row[F_PART].strip(),
                    "serial_number": row[F_SN].strip(),
                    "elector_name": name,
                    "relation_name": rel_name,
                    "relation_type": rel,
                    "sex": sex,
                    "age": age,
                    "locality": row.get("locality", "").strip() if not old_schema else "",
                    "roll_year": row.get("roll_year", "2005").strip(),
                }
                writer.writerow(out)

        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {stats['total']:,} rows total")
    print(f"  {stats['sex_mapped']:,} sex values mapped (Bengali → M/F)")
    print(f"  {stats['col_shift_fixed']:,} column-shifted rows fixed")
    print(f"  {stats['rel_extracted_from_relname']:,} relation types extracted from relation_name")
    print(f"  {stats['rel_extracted_from_name']:,} relation types extracted from elector_name suffix")
    print(f"  {stats['rel_already_set']:,} relation types already set")
    remaining_empty = (stats["total"] - stats["rel_already_set"]
                       - stats["rel_extracted_from_relname"]
                       - stats["rel_extracted_from_name"])
    print(f"  {remaining_empty:,} rows still without relation type")


# -- Transliteration -------------------------------------------------------

_REL_EN = {
    "িপ": "Father",
    "স্ব্য": "Husband",
    "স্বঁ": "Husband",
    "স্বা": "Husband",
    "ম্য": "Mother",
    "মঁ": "Mother",
    "মা": "Mother",
    "অ": "Other",
}

_SEX_EN = {"M": "M", "F": "F"}


def _reorder_bengali_matras(text):
    """Fix pre-base matras (ি, ে, ৈ) that appear before their consonant.

    In garbled source text, these vowel signs sometimes appear *before* the
    consonant they modify (e.g. েদ instead of দে).  Only swap when the matra
    is NOT already preceded by a consonant — that way correct sequences like
    রেশ (r-e-sh) are left untouched.
    """
    return re.sub(
        r'(?<![\u0995-\u09B9\u09DC-\u09DF])'   # not preceded by consonant
        r'([\u09BF\u09C7\u09C8])'               # pre-base matra
        r'([\u0995-\u09B9\u09DC-\u09DF])',       # followed by consonant
        r'\2\1',
        text,
    )


def _transliterate_bengali(text):
    """Transliterate Bengali text to approximate English via IAST."""
    if not text or not text.strip():
        return ''
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate

    cleaned = text.strip()

    # Fix garbled pre-base matra ordering (ে/ৈ/ি before consonant)
    cleaned = _reorder_bengali_matras(cleaned)

    # Chandrabindu (ঁ U+0981) → anusvara (ং U+0982) for proper nasalisation
    cleaned = cleaned.replace('\u0981', '\u0982')

    # Handle nukta combinations before IAST conversion
    # ড় → r, ঢ় → rh, য় → y  (nukta U+09BC)
    cleaned = cleaned.replace('ড়', 'র')   # ড + nukta → treat as র (r)
    cleaned = cleaned.replace('ঢ়', 'র')   # ঢ + nukta → treat as র (r)
    cleaned = cleaned.replace('য়', 'য')   # য + nukta → treat as য (y)
    cleaned = cleaned.replace('\u09BC', '')  # strip any remaining nukta

    iast = transliterate(cleaned, sanscript.BENGALI, sanscript.IAST)

    # Strip inherent schwa at end of each word BEFORE diacritic flattening,
    # so we can distinguish short 'a' (inherent, usually silent word-finally
    # in Bengali) from long 'ā' (explicit vowel sign া, should be kept).
    words = iast.split()
    result = []
    for w in words:
        if len(w) > 1 and w.endswith('a') and not w.endswith('āa'):
            w = w[:-1]
        result.append(w)
    iast = ' '.join(result)

    diacritic_map = {
        'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
        'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
        'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm',
    }
    for k, v in diacritic_map.items():
        iast = iast.replace(k, v)
    return iast.title()


def _transliterate_csv(csv_path):
    """Add English transliteration columns to an existing Tripura CSV."""
    import tempfile, shutil

    with open(csv_path, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        old_fields = list(reader.fieldnames)

    # If already transliterated, re-transliterate (overwrite columns)
    already_done = 'elector_name_en' in old_fields
    if already_done:
        new_fields = old_fields
    else:
        new_fields = old_fields + ['elector_name_en', 'relation_name_en',
                                    'relation_type_en', 'sex_en']

    tmp = tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                      dir=os.path.dirname(csv_path) or '.',
                                      suffix='.csv', delete=False)
    try:
        with open(csv_path, encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=new_fields)
            writer.writeheader()
            count = 0
            for row in reader:
                row['elector_name_en'] = _transliterate_bengali(row.get('elector_name', ''))
                row['relation_name_en'] = _transliterate_bengali(row.get('relation_name', ''))
                row['relation_type_en'] = _REL_EN.get(row.get('relation_type', '').strip(), '')
                row['sex_en'] = _SEX_EN.get(row.get('sex', '').strip(), '')
                writer.writerow(row)
                count += 1
                if count % 500_000 == 0:
                    print(f"  {count:,} rows...", flush=True)
            tmp.close()
            shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Transliterate {csv_path}: {count:,} rows, 4 columns added")


# -- Main ------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Extract Tripura SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/tripura")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to normalise schema and fix fields")
    ap.add_argument("--transliterate", metavar="CSV", default=None,
                    help="add English transliteration columns to an existing CSV")
    args = ap.parse_args()

    if args.transliterate:
        _transliterate_csv(args.transliterate)
        return

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
