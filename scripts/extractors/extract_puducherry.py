"""
Extract Puducherry old SIR (2002) voter roll PDFs to CSV.

PDF uses Tamil CIDFont. Text extracted as-is using word-position
column detection. Column number markers (1)(2)...(8) or 1 2...8 used
for alignment.

Usage:
    python scripts/extractors/extract_puducherry.py                    # all downloaded ACs
    python scripts/extractors/extract_puducherry.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_puducherry.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_puducherry.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

STATE_ID = "puducherry"
ROLL_YEAR = 2002
N_COLS = 8
NARROW_COLS = {4, 6, 7}
ROW_TOL = 5.0

# Fallback column centres when (1)(2)...(8) markers are garbled by OCR.
# Derived from actual word positions in Puducherry SIR PDFs (Tamil CIDFont):
#   1=serial@84, 2=house@110, 3=name@150, 4=relation@278,
#   5=rel_name@301, 6=sex@425, 7=age@449, 8=epic@490
FALLBACK_CENTRES = {1: 84, 2: 110, 3: 150, 4: 278, 5: 301, 6: 425, 7: 449, 8: 490}
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
    col_centres = _find_column_row(words) or fallback_centres or FALLBACK_CENTRES
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


# -- Main ------------------------------------------------------------------


# ── CSV post-processing ──────────────────────────────────────────────────

# Puducherry is multi-language: Tamil, Telugu, Malayalam, plus garbled fonts.
# Male indicators (any script): ஆ, Gu, ഡു, ഡൂ, and Telugu patterns with MC
# Female indicators: பெ, Qu, ത്രീ, and Telugu patterns with Hý
# Relation: த=father (Tamil), க=husband (Tamil), తం=father (Telugu), భ=husband (Telugu)

_MALE_PREFIXES = ('ஆ', 'Gu', 'GG', 'ഡു', 'ഡൂ', 'a')
_FEMALE_PREFIXES = ('பெ', 'Qu', 'QQ', 'த்ரீ', 'ത്രീ')
_MALE_CONTAINS = ('MC',)  # Telugu male pattern
_FEMALE_CONTAINS = ('Hý', 'íHý')  # Telugu female pattern

_REL_MAP = {
    'த': 'த', 'தத': 'த', 'த.': 'த',    # Father (Tamil)
    'க': 'க', 'கக': 'க', 'க.': 'க',    # Husband (Tamil)
    'தா': 'தா',                           # Mother (Tamil)
    'ஏ': 'ஏ', 'ஏ.': 'ஏ',                 # Other (Tamil)
    'తం': 'తం',                           # Father (Telugu)
    'భ': 'భ',                             # Husband (Telugu)
    '': '',
}


def _classify_sex(val):
    """Classify sex value from multi-script Puducherry data."""
    if not val:
        return ''
    if val in ('M', 'F'):
        return val
    for p in _MALE_PREFIXES:
        if val.startswith(p):
            return 'M'
    for p in _FEMALE_PREFIXES:
        if val.startswith(p):
            return 'F'
    for c in _MALE_CONTAINS:
        if c in val:
            return 'M'
    for c in _FEMALE_CONTAINS:
        if c in val:
            return 'F'
    return ''


def _dedouble(s):
    """Remove character-doubling caused by Yanam OCR (each char printed twice).

    E.g. "3322" -> "32", "HHDDFFO00117700330088" -> "HDFO0170308".
    Only applied when the string shows a clear doubling pattern (>= 60% paired).
    """
    if not s or len(s) < 2:
        return s
    # Count how many chars are in consecutive identical pairs
    pairs = 0
    i = 0
    while i + 1 < len(s):
        if s[i] == s[i + 1]:
            pairs += 1
            i += 2
        else:
            i += 1
    # Require at least 60% of characters to be in doubled pairs
    if pairs * 2 < len(s) * 0.6:
        return s
    # De-double: consume pairs, keep singletons
    result = []
    i = 0
    while i < len(s):
        if i + 1 < len(s) and s[i] == s[i + 1]:
            result.append(s[i])
            i += 2
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


_TAMIL_DIGITS = str.maketrans('௦௧௨௩௪௫௬௭௮௯', '0123456789')


def _clean_age(val):
    """Extract a valid numeric age from OCR-garbled age values.

    Strips OCR artifacts like ], %, ), . and non-digit characters.
    Translates Tamil numerals (௦-௯) to ASCII digits.
    Returns a valid age string (18-120) or empty string.
    Ages below 18 are treated as unrecoverable (voter roll minimum age).
    """
    if not val:
        return ""
    # Translate Tamil numerals to ASCII
    val = val.translate(_TAMIL_DIGITS)
    # Already clean numeric
    if val.isdigit():
        age = int(val)
        if 18 <= age <= 120:
            return str(age)
        # Doubled age from Yanam that survived (e.g. "2209" -> digits "2209")
        if age > 120 and len(val) >= 2:
            trunc = int(val[:2])
            if 18 <= trunc <= 120:
                return str(trunc)
        return ""
    # Strip non-digit OCR artifacts
    cleaned = re.sub(r'[^\d]', '', val)
    if not cleaned:
        return ""
    age = int(cleaned)
    if 18 <= age <= 120:
        return str(age)
    # Too large - try first two digits
    if len(cleaned) >= 2:
        trunc = int(cleaned[:2])
        if 18 <= trunc <= 120:
            return str(trunc)
    return ""


def _repair_shifted_row(fields, expected_n):
    """Repair a CSV row that has too many fields due to comma-in-field.

    The 14 expected columns (CSV_HEADERS) are:
      0:state 1:district 2:ac_no 3:ac_name 4:part_no 5:serial_no
      6:house_no 7:elector_name 8:relation 9:relation_name 10:sex
      11:age 12:epic_no 13:roll_year

    Strategy: anchor from both ends. The last field (roll_year) and first
    6 fields (state..serial_no) are reliably comma-free. The extra commas
    are in the text fields between them (house_no through epic_no).
    We keep the tail 4 fields (sex, age, epic_no, roll_year) anchored
    and merge the surplus into the middle text fields.
    """
    if len(fields) <= expected_n:
        return fields
    extra = len(fields) - expected_n
    head = fields[:6]       # state, district, ac_no, ac_name, part_no, serial_no
    tail4 = fields[-4:]     # sex, age, epic_no, roll_year
    middle = fields[6:-4]   # should be 4 (house, name, relation, rel_name) but has 4+extra

    # The 4 middle targets: house_no, elector_name, relation, relation_name
    # 'relation' is usually short (1-2 chars). Try to find it.
    if len(middle) >= 4:
        # Scan from the end: relation_name is last, relation is second-to-last
        rel_name = middle[-1]
        rel_code = middle[-2]
        # Check if rel_code looks like a relation code
        rc = rel_code.strip()
        is_rel = (len(rc) <= 3 and rc != "") or rc in _REL_MAP
        if is_rel:
            # Everything before the relation pair is house_no + elector_name
            before_rel = middle[:-2]
            house = before_rel[0] if before_rel else ""
            ename = " ".join(before_rel[1:]) if len(before_rel) > 1 else ""
            return head + [house, ename, rel_code, rel_name] + tail4
        else:
            # Relation may have been lost; merge all surplus into elector_name
            house = middle[0]
            rel_name = middle[-1]
            rel_code = middle[-2] if len(middle) >= 3 else ""
            ename = " ".join(middle[1:-2]) if len(middle) > 2 else ""
            return head + [house, ename, rel_code, rel_name] + tail4
    # Fewer than 4 middle fields shouldn't happen, but pad
    while len(middle) < 4:
        middle.append("")
    return head + middle[:4] + tail4


def _is_header_row(fields):
    """Detect header/metadata rows that leaked through extraction.

    These are page headers, summary lines, or garbled non-voter text that
    got a serial number assigned by the column parser.  They share a common
    trait: they have text in elector_name but NO sex, NO age, NO relation,
    and NO relation_name — real voters virtually always have at least a
    relation or relation_name even when age/sex are OCR-garbled.

    Additionally, rows with completely empty elector_name AND relation_name
    are dropped (no recoverable voter data).
    """
    # Index references: 7=elector_name, 8=relation, 9=relation_name,
    #                   10=sex, 11=age
    name = fields[7].strip()
    rel = fields[8].strip()
    rel_name = fields[9].strip()
    sex = fields[10].strip()
    age = fields[11].strip()

    # --- Rule 1: empty elector_name → no recoverable voter identity ---
    if not name:
        return True

    # --- Rule 2: known header text patterns in elector_name ---
    # These are page headers / summary lines that sometimes have stray
    # numbers bleeding into the relation_name column, so we match on
    # elector_name alone (only requiring no sex + no age).
    _HEADER_SUBSTRINGS = (
        'முதன்மை',         # "Primary" page header
        'சுருக்கமுறை',     # "Summary" header (Tamil)
        'சுருக்கமுழை',     # OCR variant of "Summary" (Tamil)
        'சசுுரருுகக',      # Doubled "Summary" (Yanam)
        'அடிப்படைப்',     # "Base list" header (Tamil)
        'அஅடடிிபப்',      # Doubled "Base list" (Yanam)
        'மமுுததனன்',       # Doubled "Primary" (Yanam)
        'மமுுததலல்',       # Doubled variant (Yanam)
        'സംക്ഷിപ്ത',      # "Summary" (Malayalam)
        'SHY నదరణ',        # Telugu header
        'aQeayon',         # Garbled font header
        'äæSêþC',          # Garbled font header
        'qe iä',           # Garbled font header
        'சேர்த்தல் பட்டியல்',  # "Addition list"
        'திருத்தத்தின்',   # "Amendment details"
    )
    if name and not sex and not age:
        for sub in _HEADER_SUBSTRINGS:
            if sub in name:
                return True

    # --- Rule 3: remaining header-like rows with no voter data at all ---
    if name and not sex and not age and not rel and not rel_name:
        # Multi-word title with no digits (e.g. school names, area names)
        words = name.split()
        if len(words) >= 3 and not any(c.isdigit() for c in name):
            return True

    return False


def _postprocess(csv_path):
    """Fix extraction artifacts in an already-extracted CSV.

    Puducherry-specific fixes (multi-language: Tamil, Telugu, Malayalam):
    0. Header/metadata row removal: page headers, summary lines, and
       garbled non-voter text that leaked through extraction are dropped.
    1. CSV column shift: rows with 15+ fields from comma-in-field are
       repaired by re-joining shifted fields.
    2. Yanam (AC030) character-doubling: OCR produces each character twice
       in serial_no, age, and epic_no. De-duplicated.
    3. Age cleanup: strip OCR artifacts (], %, ), .), extract numeric age.
       Invalid ages set to empty.
    4. Sex classification: multi-script sex values mapped to M/F.
    5. Relation cleanup: map to canonical forms.
    """
    import tempfile, shutil

    fixes = {
        "total": 0, "dropped_header": 0, "sex_cleaned": 0, "rel_cleaned": 0,
        "age_cleaned": 0, "age_emptied": 0,
        "yanam_dedoubled": 0, "col_shifted_repaired": 0,
    }
    expected_n = len(CSV_HEADERS)  # 14

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            writer = csv.writer(tmp)
            writer.writerow(header)

            for fields in reader:
                fixes["total"] += 1

                # --- Fix 1: column shift (too many fields) ---
                if len(fields) > expected_n:
                    fields = _repair_shifted_row(fields, expected_n)
                    fixes["col_shifted_repaired"] += 1

                # Pad if too few fields
                while len(fields) < expected_n:
                    fields.append("")

                # --- Fix 0: drop header/metadata rows ---
                if _is_header_row(fields):
                    fixes["dropped_header"] += 1
                    continue

                # Index references (CSV_HEADERS):
                #   2=ac_no, 5=serial_no, 8=relation, 10=sex, 11=age, 12=epic_no
                ac_no_str = fields[2].strip()

                # --- Fix 2: Yanam (AC030) character de-doubling ---
                if ac_no_str == "30":
                    orig_serial = fields[5]
                    orig_age = fields[11]
                    orig_epic = fields[12]
                    fields[5] = _dedouble(fields[5].strip())
                    fields[11] = _dedouble(fields[11].strip())
                    fields[12] = _dedouble(fields[12].strip())
                    if (fields[5] != orig_serial or fields[11] != orig_age
                            or fields[12] != orig_epic):
                        fixes["yanam_dedoubled"] += 1

                # --- Fix 3: age cleanup ---
                raw_age = fields[11].strip()
                cleaned_age = _clean_age(raw_age)
                if cleaned_age != raw_age:
                    if cleaned_age == "":
                        fixes["age_emptied"] += 1
                    else:
                        fixes["age_cleaned"] += 1
                    fields[11] = cleaned_age

                # --- Fix 4: sex classification ---
                sex = fields[10].strip()
                classified = _classify_sex(sex)
                if classified != sex:
                    fields[10] = classified
                    fixes["sex_cleaned"] += 1

                # --- Fix 5: relation cleanup ---
                rel = fields[8].strip()
                if rel in _REL_MAP:
                    fields[8] = _REL_MAP[rel]
                elif rel and rel[0] in ('\u0ba4', '\u0c15', '\u0b95'):
                    # Tamil/Telugu father/husband with suffix bleed
                    fields[8] = rel[0]
                    fixes["rel_cleaned"] += 1
                elif rel:
                    fields[8] = ""
                    fixes["rel_cleaned"] += 1

                writer.writerow(fields)

        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['dropped_header']:,} header/metadata rows dropped")
    print(f"  {fixes['col_shifted_repaired']:,} column-shifted rows repaired")
    print(f"  {fixes['yanam_dedoubled']:,} Yanam rows de-doubled")
    print(f"  {fixes['age_cleaned']:,} ages cleaned, {fixes['age_emptied']:,} ages emptied")
    print(f"  {fixes['sex_cleaned']:,} sex values fixed")
    print(f"  {fixes['rel_cleaned']:,} relation values fixed")


# -- Transliteration -------------------------------------------------------

_REL_EN = {
    'த': 'Father',
    'க': 'Husband',
    'தா': 'Mother',
    'ஏ': 'Other',
    'తం': 'Father',
    'భ': 'Husband',
    '': '',
}

_SEX_EN = {'M': 'M', 'F': 'F'}


def _detect_script(text):
    """Detect dominant Indic script in text."""
    from indic_transliteration import sanscript
    tamil = len(re.findall(r'[\u0B80-\u0BFF]', text))
    telugu = len(re.findall(r'[\u0C00-\u0C7F]', text))
    malayalam = len(re.findall(r'[\u0D00-\u0D7F]', text))
    devanagari = len(re.findall(r'[\u0900-\u097F]', text))
    if tamil >= max(telugu, malayalam, devanagari, 1):
        return sanscript.TAMIL
    if telugu >= max(tamil, malayalam, devanagari, 1):
        return sanscript.TELUGU
    if malayalam >= max(tamil, telugu, devanagari, 1):
        return sanscript.MALAYALAM
    if devanagari >= max(tamil, telugu, malayalam, 1):
        return sanscript.DEVANAGARI
    return None  # likely already English


# Malayalam chillu characters -> consonant + virama (indic_transliteration
# doesn't handle chillus natively)
_CHILLU_MAP = {
    '\u0D7A': '\u0D23\u0D4D',  # ൺ -> ണ്
    '\u0D7B': '\u0D28\u0D4D',  # ൻ -> ന്
    '\u0D7C': '\u0D30\u0D4D',  # ർ -> ര്
    '\u0D7D': '\u0D32\u0D4D',  # ൽ -> ല്
    '\u0D7E': '\u0D33\u0D4D',  # ൾ -> ള്
    '\u0D7F': '\u0D15\u0D4D',  # ൿ -> ക്
}


_TAMIL_VOWELS = {
    'அ': 'a', 'ஆ': 'a', 'இ': 'i', 'ஈ': 'i',
    'உ': 'u', 'ஊ': 'u', 'எ': 'e', 'ஏ': 'e',
    'ஐ': 'ai', 'ஒ': 'o', 'ஓ': 'o', 'ஔ': 'au',
}

_TAMIL_CONSONANTS = {
    'க': 'k', 'ங': 'n', 'ச': 's', 'ஞ': 'nj',
    'ட': 't', 'ண': 'n', 'த': 'th', 'ந': 'n',
    'ப': 'p', 'ம': 'm', 'ய': 'y', 'ர': 'r',
    'ல': 'l', 'வ': 'v', 'ழ': 'zh', 'ள': 'l',
    'ற': 'r', 'ன': 'n', 'ஷ': 'sh', 'ஸ': 's',
    'ஹ': 'h', 'ஜ': 'j',
}

# Vowel signs (matras) — long/short collapsed for search-friendly output
_TAMIL_MATRAS = {
    '\u0BBE': 'a',   # ா
    '\u0BBF': 'i',   # ி
    '\u0BC0': 'i',   # ீ
    '\u0BC1': 'u',   # ு
    '\u0BC2': 'u',   # ூ
    '\u0BC6': 'e',   # ெ
    '\u0BC7': 'e',   # ே
    '\u0BC8': 'ai',  # ை
    '\u0BCA': 'o',   # ொ
    '\u0BCB': 'o',   # ோ
    '\u0BCC': 'au',  # ௌ
}

_TAMIL_VIRAMA = '\u0BCD'  # ்


def _transliterate_tamil(text):
    """Custom Tamil-to-Latin transliteration.

    indic_transliteration's IAST output for Tamil is wrong — it applies
    Sanskrit conventions (aspirated consonants: க→gha, ட→ḍha) that don't
    exist in Tamil.  This function uses correct Tamil romanization: க→k,
    ச→s, ட→t, த→th, ப→p, no aspiration.
    """
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _TAMIL_VOWELS:
            result.append(_TAMIL_VOWELS[ch])
            i += 1
        elif ch in _TAMIL_CONSONANTS:
            cons = _TAMIL_CONSONANTS[ch]
            if i + 1 < len(text) and text[i + 1] == _TAMIL_VIRAMA:
                # Consonant + virama = bare consonant (no inherent vowel)
                result.append(cons)
                i += 2
            elif i + 1 < len(text) and text[i + 1] in _TAMIL_MATRAS:
                result.append(cons + _TAMIL_MATRAS[text[i + 1]])
                i += 2
            else:
                # Bare consonant with inherent 'a'
                result.append(cons + 'a')
                i += 1
        elif ch in _TAMIL_MATRAS:
            # Orphan matra (shouldn't happen in well-formed text)
            result.append(_TAMIL_MATRAS[ch])
            i += 1
        elif ch == _TAMIL_VIRAMA:
            i += 1
        elif ch == ' ':
            result.append(' ')
            i += 1
        elif ch.isascii():
            result.append(ch)
            i += 1
        else:
            # Skip unknown characters (ZWNJ etc. already stripped upstream)
            i += 1
    return ''.join(result)


def _transliterate_multi(text):
    """Transliterate Tamil/Telugu/Malayalam/Devanagari text to English."""
    if not text or not text.strip():
        return ''
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate
    script = _detect_script(text)
    if script is None:
        return text.strip().title()  # already English
    # Strip zero-width joiners before transliterating
    t = text.strip().replace('\u200C', '').replace('\u200D', '')
    # Tamil: use custom transliteration (library produces wrong aspirates)
    if script == sanscript.TAMIL:
        return ' '.join(_transliterate_tamil(t).split()).title()
    # Replace Malayalam chillus before transliteration
    if script == sanscript.MALAYALAM:
        for chillu, repl in _CHILLU_MAP.items():
            t = t.replace(chillu, repl)
    iast = transliterate(t, script, sanscript.IAST)
    diacritic_map = {
        'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
        'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
        'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm', 'ṉ': 'n',
        'ḻ': 'l', 'ṟ': 'r',
        'è': 'e', 'ò': 'o',  # short e/o used by indic_transliteration
    }
    for k, v in diacritic_map.items():
        iast = iast.replace(k, v)
    return ' '.join(iast.split()).title()


def _transliterate_csv(csv_path):
    """Add English transliteration columns to an existing Puducherry CSV."""
    import tempfile, shutil

    with open(csv_path, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        old_fields = list(reader.fieldnames)

    # If already transliterated, skip
    if 'elector_name_en' in old_fields:
        print(f"Already transliterated: {csv_path}")
        return

    new_fields = old_fields + ['elector_name_en', 'relation_name_en',
                                'relation_type_en', 'sex_en']

    tmp = tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                      dir=os.path.dirname(csv_path),
                                      suffix='.csv', delete=False)
    try:
        with open(csv_path, encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=new_fields)
            writer.writeheader()
            count = 0
            for row in reader:
                row['elector_name_en'] = _transliterate_multi(row.get('elector_name', ''))
                row['relation_name_en'] = _transliterate_multi(row.get('relation_name', ''))
                row['relation_type_en'] = _REL_EN.get(row.get('relation', '').strip(), '')
                row['sex_en'] = _SEX_EN.get(row.get('sex', '').strip(), '')
                writer.writerow(row)
                count += 1
                if count % 100_000 == 0:
                    print(f"  {count:,} rows transliterated...", flush=True)
        tmp.close()
        shutil.move(tmp.name, csv_path)
        print(f"Transliterated {csv_path}: {count:,} rows, added elector_name_en/relation_name_en/relation_type_en/sex_en")
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


def main():
    ap = argparse.ArgumentParser(description="Extract Puducherry SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/puducherry")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to fix column bleeding")
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
