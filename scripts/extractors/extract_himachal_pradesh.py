"""
Extract Himachal Pradesh old SIR (2002) voter roll PDFs to CSV.

PDF uses DVTTSurekh legacy Devanagari. Text extracted as-is using word-position
column detection. Column number markers (1)(2)...(8) or 1 2...8 used for alignment.

Usage:
    python scripts/extractors/extract_himachal_pradesh.py                    # all downloaded ACs
    python scripts/extractors/extract_himachal_pradesh.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_himachal_pradesh.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_himachal_pradesh.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
import zipfile

import pdfplumber

STATE_ID = "himachal_pradesh"
ROLL_YEAR = 2002
N_COLS = 8
NARROW_COLS = {4, 6, 7}
ROW_TOL = 5.0


# ── DVTTSurekh → Unicode Devanagari transcoder ──────────────────────────
# DVTTSurekh uses the same byte-to-glyph encoding as DK-RAJ (Haryana).
# Consonants are stored as half forms (C + virama); a stem glyph (mapped
# to ा) completes them.  Pre-base ि is typed before its cluster and
# must be reordered.  See states/haryana_dkraj.py for full documentation.

_REPHA = '\ue000'
_REPHA_DONE = '\ue001'
_IREPHA = '\ue002'
_I_PLACED = '\ue003'

_MAP = {
    ' ': ' ', '!': '!', '(': '(', ')': ')', ',': ',', '-': '-', '.': '.', '/': '/',
    ':': ':', ';': ';', '?': '?', '&': 'ः', '%': 'ऽ', '$': 'ॐ', "'": ',',
    '*': 'ा', '+': 'अ', '<': 'इ', '=': 'उ', '>': 'ऊ',
    '@': 'ऋ', 'A': 'ॠ', 'B': 'ए',
    **{c: c for c in '0123456789'},
    'C': 'क्', 'E': 'क्', 'F': 'क़्',
    'G': 'क्र्', 'H': 'क्त्', 'I': 'क्ष्',
    'J': 'ख्', 'K': 'ख़्', 'L': 'ख़्', 'M': 'ग्', 'N': 'ग़्',
    'O': 'ग्र्', 'P': 'घ्', 'Q': 'घ़्',
    'S': 'च्', 'U': 'छ्', 'V': 'ज्', 'W': 'ज़्',
    'Y': 'ज्ञ्', 'Z': 'झ्',
    ']': 'ट्', '`': 'ठ्', 'b': 'ड्', 'c': 'ड़्',
    'f': 'ढ्', 'g': 'ढ़्', 'h': 'ण्',
    'i': 'त्', 'j': 'त्र्', 'k': 'त्त्',
    'l': 'थ्', 'n': 'द्', 'o': 'द्र्',
    'p': 'द्र्', 'q': 'द्द्', 'r': 'द्ध्', 's': 'ब्र्',
    't': 'द्य्', 'u': 'द्व्',
    'v': 'ध्', 'x': 'न्', 'z': 'न्न्',
    '{': 'प्', '|': 'प्र्', '}': 'फ्',
    '\xa1': 'फ्', '\xa2': 'फ़्', '\xa3': 'फ्र्',
    '\xa4': 'ब्', '\xa5': 'ब्र्', '\xa6': 'भ्', '\xa8': 'म्',
    '\xaa': 'य्', '\xae': 'र', '\xaf': 'र', '\xb0': 'र',
    '\xb1': 'ल्', '\xb4': 'व्',
    '\xb6': 'श्', '\xb7': 'श्व्', '\xb8': 'श्र्', '\xb9': 'ष्',
    '\xba': 'स्', '\xbb': 'स्र्', '\xbd': 'ह्',
    '\xc0': 'ह्म्',
    '\xc2': '्', '\xc3': '', '\xc4': 'ँ', '\xc5': 'ँ', '\xc6': 'ं',
    '\xc7': _REPHA, '\xc8': _REPHA,
    '\xc9': 'ा', '\xca': 'ि', '\xcb': 'िं', '\xcc': _IREPHA,
    '\xcd': 'िं', '\xce': 'ि', '\xcf': 'िं',
    '\xd2': 'ी', '\xd3': 'ीं', '\xd4': 'ी' + _REPHA,
    '\xd0': 'ू', '\xd6': 'ु', '\xd9': 'ु', '\xda': 'ू', '\xde': 'ृ',
    '\xe0': 'े', '\xe4': 'े', '\xe5': 'ें', '\xe6': 'े' + _REPHA,
    '\xe7': 'ें' + _REPHA, '\xe8': 'ै', '\xe9': 'ैं',
    '\xea': 'ै' + _REPHA, '\xeb': 'ं' + _REPHA, '\xec': 'ॉ',
    '\xf0': '', '\xf1': '',
    '\xf2': 'ा', '\xf3': 'ा', '\xf4': 'ा', '\xf5': 'ा', '\xf6': 'ा',
    '\xf7': 'ा', '\xf8': 'ा', '\xf9': 'ा', '\xfa': '',
    '\xfb': 'ा', '\xfc': 'ू', '\xfe': 'ा',
}

_CONS = '[\u0915-\u0939\u0958-\u0961]'
_CLUSTER = '(?:' + _CONS + '्)*' + _CONS
_MATRAS = '[\u093E-\u094C\u0902\u0903\u0901\u0943]'
_INNER = '[\u093F-\u094C\u0902\u0903\u0901\u0943]'
_AA = 'ा'
_VIRAMA = '्'


def _dvtt_decode(s):
    """Transcode a DVTTSurekh string to Unicode Devanagari."""
    out = []
    for ch in s:
        if ch in _MAP:
            out.append(_MAP[ch])
        elif ch.isascii() and (ch.isdigit() or ch.isalpha() or ch in '()[]{}/.,:;-'):
            out.append(ch)
        else:
            out.append('�')
    t = ''.join(out)
    t = re.sub(_VIRAMA + '(' + _INNER + '*)' + _REPHA, _VIRAMA + _REPHA + r'\1', t)
    t = re.sub('(' + _CONS + ')' + _VIRAMA + _REPHA + '(' + _INNER + '*?)' + _AA,
               _REPHA_DONE + r'\1\2', t)
    t = re.sub(_VIRAMA + '(' + _INNER + '*?)' + _AA, r'\1', t)
    for a, b in (('ाे', 'ो'), ('ाै', 'ौ'), ('ाॉ', 'ॉ'),
                 ('अा', 'आ'), ('अो', 'ओ'), ('अौ', 'औ'),
                 ('अे', 'ए'), ('अै', 'ऐ')):
        t = t.replace(a, b)
    t = re.sub(_IREPHA + '(' + _CLUSTER + ')', _REPHA_DONE + r'\1' + _I_PLACED, t)
    t = re.sub('िं(' + _CLUSTER + ')', r'\1िं', t)
    t = re.sub('ि(' + _CLUSTER + ')', r'\1ि', t)
    t = re.sub('(' + _CLUSTER + ')(' + _MATRAS + '*)' + _REPHA, _REPHA_DONE + r'\1\2', t)
    t = t.replace('\u0907' + _REPHA, 'ई')
    t = t.replace(_REPHA_DONE, 'र्').replace(_REPHA, 'र्').replace(_I_PLACED, 'ि')
    return unicodedata.normalize('NFC', t)
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

    DVTTSurekh text is non-ASCII, so "alphabetic" checks must look for
    absence of digits rather than ASCII letters.
    """
    house = row[_HOUSE].strip()
    name = row[_NAME].strip()
    rel = row[_REL].strip()

    # House_no with NO digits at all -> it's a name fragment, not a house number
    if house and not re.search(r"\d", house):
        name = f"{house} {name}".strip()
        house = ""

    # Trailing non-digit text after a digit-based house number -> name fragment
    if house:
        m = re.match(r"^(.*?\d[\d/\-]*)\s+(\S.*)$", house)
        if m:
            house = m.group(1).strip()
            name = f"{m.group(2).strip()} {name}".strip()

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
            # Decode DVTTSurekh text to Unicode Devanagari (skip serial, age, epic)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _dvtt_decode(cells[idx])
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


# -- CSV post-processing -------------------------------------------------------

VALID_SEX = {"पुरूष", "महिला", "अन्य", ""}
VALID_RELATION = {"पति", "पिता", "माता", "अन्य", ""}


def _split_house_name(house_no):
    """Split a house_no that has an elector name absorbed into it.

    Common patterns from DVTTSurekh mis-assignment:
      "8(1) थाचुंग"    → house="8(1)",    name="थाचुंग"
      "20क वंगडुव"     → house="20क",     name="वंगडुव"
      "10/1(3) मीना"   → house="10/1(3)",  name="मीना"
      "85(ख) सीता"     → house="85(ख)",   name="सीता"
      "38क ,प्रेम"     → house="38क",     name="प्रेम"
      "46/1, संदीपा"   → house="46/1",    name="संदीपा"
      "672ए कपिल"      → house="672ए",    name="कपिल"
      "61वी राजेन्द्र"  → house="61वी",    name="राजेन्द्र"
      "32/1ए राकेश"    → house="32/1ए",   name="राकेश"

    Returns (house_part, name_part).  name_part is "" if no split needed.
    """
    h = house_no.strip()
    if not h:
        return h, ""

    # Devanagari character classes used in sub-units:
    #   consonants, vowel letters, matras, nukta, virama, anusvara, visarga
    _DEV_SUBUNIT = r'[\u0900-\u097F�]'  # any Devanagari + replacement char

    # General pattern: digits/slashes/hyphens, optional Devanagari sub-unit
    # suffix (1-3 chars like क, ए, वी, ाक, etc.), optional parenthesized
    # part (with optional sub-unit after it too), then separator, then the
    # actual name (starts with Devanagari).
    m = re.match(
        r'^('                              # --- house part ---
        r'[\d/\-]+'                        # leading digits/slashes/hyphens
        r'(?:[,.][\d/\-]+)*'               # e.g. ",1" in "28/,1"
        r'(?:' + _DEV_SUBUNIT + r'{0,3})'  # optional Devanagari sub-unit before paren
        r'(?:\([^)]*\))?'                  # optional parenthesized part like (1) or (ख)
        r'(?:' + _DEV_SUBUNIT + r'{0,3})'  # optional Devanagari sub-unit after paren
        r')'
        r'[,\s]+'                          # separator: comma and/or whitespace
        r'([\u0900-\u097F].+)$',           # name: starts with Devanagari, 2+ chars
        h
    )
    if m:
        return m.group(1).strip().rstrip(','), m.group(2).strip()

    # Handle: "8/ॠ विपन" or "6-ॠ ,मीना" — Devanagari sub-unit after / or -
    m = re.match(
        r'^('
        r'[\d/\-]+'
        r'[/\-]'
        + _DEV_SUBUNIT + r'{1,3}'          # sub-unit after / or -
        r')'
        r'[,\s]+'
        r'([\u0900-\u097F].+)$',
        h
    )
    if m:
        return m.group(1).strip().rstrip(','), m.group(2).strip()

    # Fallback: any house_no with a space/comma followed by Devanagari
    # text — handles garbled OCR like "37/1. सुमन", "30/? झुलरा", etc.
    m = re.match(
        r'^(\S+?)'                         # house part (non-whitespace)
        r'[,\s]+'                          # separator
        r'([\u0900-\u097F][\u0900-\u097F\s]+)$',  # name: 2+ Devanagari chars
        h
    )
    if m:
        return m.group(1).strip().rstrip(','), m.group(2).strip()

    # Last resort: digit(s) immediately followed by 2+ Devanagari chars
    # with no separator, e.g. "5माया", "2पूनम"
    m = re.match(
        r'^(\d+)'                          # house number (digits only)
        r'([\u0915-\u0939\u0958-\u0961]'   # first Devanagari consonant
        r'[\u0900-\u097F]+)$',             # rest of name (2+ total Devanagari)
        h
    )
    if m:
        return m.group(1), m.group(2)

    return h, ""


def _postprocess(csv_path):
    """Clean an already-extracted HP CSV:

    1. Name absorbed into house_no — split house_no, move name fragment
       to elector_name (prepend if elector_name already has a partial).
    2. Commas inside house_no — same split logic handles these.
    3. Invalid sex / relation values — clear to empty.
    4. Bad ages (negative, > 120, non-numeric) — clear to empty.
    """
    import tempfile, shutil

    fixes = {
        "total": 0,
        "name_from_house": 0,
        "sex_cleaned": 0,
        "rel_cleaned": 0,
        "age_cleaned": 0,
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

                # --- Fix 1: name absorbed into house_no ---
                house = row["house_no"].strip()
                name = row["elector_name"].strip()
                house_clean, name_frag = _split_house_name(house)
                if name_frag:
                    # Prepend extracted fragment to existing name
                    name = f"{name_frag} {name}".strip() if name else name_frag
                    row["house_no"] = house_clean
                    row["elector_name"] = name
                    fixes["name_from_house"] += 1

                # --- Fix 2: sex validation ---
                sex = row["sex"].strip()
                if sex not in VALID_SEX:
                    row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                # --- Fix 3: relation validation ---
                rel = row["relation"].strip()
                if rel not in VALID_RELATION:
                    row["relation"] = ""
                    fixes["rel_cleaned"] += 1

                # --- Fix 4: age validation ---
                age = row["age"].strip()
                if age:
                    try:
                        a = int(age)
                        if a < 0 or a > 120:
                            row["age"] = ""
                            fixes["age_cleaned"] += 1
                    except ValueError:
                        row["age"] = ""
                        fixes["age_cleaned"] += 1

                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['name_from_house']:,} names recovered from house_no")
    print(f"  {fixes['sex_cleaned']} sex values fixed")
    print(f"  {fixes['rel_cleaned']} relation values fixed")
    print(f"  {fixes['age_cleaned']} bad ages cleared")


# -- Hindi transliteration (Devanagari → English) -----------------------------

_RELATION_EN_MAP = {
    'पिता': 'Father', 'पि.': 'Father', 'पि-': 'Father', 'पि': 'Father',
    'पति': 'Husband', 'प.': 'Husband', 'प-': 'Husband', 'प': 'Husband',
    'माता': 'Mother', 'मा.': 'Mother', 'मा-': 'Mother', 'मा': 'Mother',
    'अन्य': 'Other', 'अ-': 'Other', 'अ.': 'Other', 'अ': 'Other',
}

_SEX_EN_MAP = {
    'पु.': 'M', 'पुरुष': 'M', 'पुरूष': 'M', 'पु-': 'M', 'M': 'M',
    'म.': 'F', 'महिला': 'F', 'स्त्री': 'F', 'म-': 'F', 'F': 'F',
}


def _transliterate_hindi(text):
    """Transliterate Devanagari text to readable English (Title Case)."""
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate as _translit

    if not text or not text.strip():
        return ''
    itrans = _translit(text.strip(), sanscript.DEVANAGARI, sanscript.ITRANS)

    result = itrans
    result = result.replace('j~n', 'gy')
    result = result.replace('~n', 'n')
    result = result.replace('.n', 'n')
    result = result.replace('.N', 'n')
    result = result.replace('RRi', 'ri').replace('R^i', 'ri')
    result = result.replace('.Dh', 'dh').replace('.D', 'd')

    # anusvara before h -> ngh (सिंह -> singh), else -> n
    result = re.sub(r'Mh', 'ngh', result)
    result = result.replace('M', 'n')

    result = result.replace('Sh', 'sh')
    result = result.replace('Ch', 'chh')
    result = result.replace('shh', 'sh')
    result = result.replace('T', 't')
    result = result.replace('D', 'd')
    result = result.replace('N', 'n')
    result = result.replace('H', 'h')

    # Long vowels: mark long-A so schwa deletion skips it
    MARKER = '\x01'
    result = result.replace('A', MARKER)
    result = result.replace('I', 'i')
    result = result.replace('U', 'u')
    result = result.replace('a' + MARKER, MARKER)

    # Schwa deletion: drop trailing inherent 'a' (unmarked) from words
    words = result.split()
    cleaned = []
    for w in words:
        if len(w) > 2 and w.endswith('a') and not w.endswith(MARKER):
            if w[-2] not in 'aeiou' + MARKER:
                w = w[:-1]
        cleaned.append(w)

    result = ' '.join(cleaned)
    result = result.replace(MARKER, 'a')
    return result.title()


def _transliterate_csv(csv_path):
    """Add elector_name_en, relation_name_en, relation_type_en, sex_en columns.

    Uses a two-pass streaming approach to avoid loading all rows into memory
    (critical for states with 30M+ rows like Rajasthan/MP).
    """
    import shutil
    import tempfile

    NEW_COLS = ['elector_name_en', 'relation_name_en', 'relation_type_en', 'sex_en']

    # Pass 1: read header and collect unique names (not full rows)
    with open(csv_path, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if any(c in reader.fieldnames for c in NEW_COLS):
            print(f"  Columns already present, skipping: {csv_path}")
            return
        out_fields = list(reader.fieldnames) + NEW_COLS

        names = set()
        total = 0
        for row in reader:
            names.add(row.get('elector_name', ''))
            names.add(row.get('relation_name', ''))
            total += 1
    names.discard('')

    print(f"  Pass 1: {total:,} rows, {len(names):,} unique names")
    print(f"  Transliterating...", end=' ', flush=True)
    cache = {'': ''}
    for n in names:
        cache[n] = _transliterate_hindi(n)
    del names  # free memory
    print("done")

    # Pass 2: stream rows, add transliterated columns, write to temp file
    tmp = tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                      dir=os.path.dirname(csv_path),
                                      suffix='.csv', delete=False)
    try:
        empty_name = 0
        with open(csv_path, encoding='utf-8') as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=out_fields)
            writer.writeheader()
            for row in reader:
                en = cache.get(row.get('elector_name', ''), '')
                row['elector_name_en'] = en
                row['relation_name_en'] = cache.get(row.get('relation_name', ''), '')
                rel = row.get('relation', '').strip()
                row['relation_type_en'] = _RELATION_EN_MAP.get(rel, '')
                sex = row.get('sex', '').strip()
                row['sex_en'] = _SEX_EN_MAP.get(sex, '')
                writer.writerow(row)
                if not en:
                    empty_name += 1
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"  {total:,} rows, {len(cache)-1:,} unique names transliterated")
    print(f"  Empty elector_name_en: {empty_name:,} ({100*empty_name/max(total,1):.1f}%)")


# -- Main ----------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Extract Himachal Pradesh SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/himachal_pradesh")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to validate sex/relation columns")
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
