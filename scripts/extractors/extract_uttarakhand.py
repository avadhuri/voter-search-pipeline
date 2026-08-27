"""
Extract Uttarakhand old SIR (2003) voter roll PDFs to CSV.

PDF uses Kruti Dev 010 legacy Hindi encoding. Text is transcoded to Unicode
Devanagari during extraction. Column number markers (1)(2)...(8) or 1 2...8
used for alignment.

Usage:
    python scripts/extractors/extract_uttarakhand.py                    # all downloaded ACs
    python scripts/extractors/extract_uttarakhand.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_uttarakhand.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_uttarakhand.py --combined         # single CSV for state
"""
import argparse
import csv
import io
import json
import os
import re
import unicodedata
import zipfile

import pdfplumber

STATE_ID = "uttarakhand"
ROLL_YEAR = 2003
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


# -- Kruti Dev 010 -> Unicode Devanagari transcoder -----------------------
#
# Kruti Dev 010 is a legacy 8-bit Hindi font widely used in Indian government
# documents. It maps ASCII and Latin-1 byte values to Devanagari glyphs.
#
# Key structural properties:
#   1. Some consonants are two-char compounds: the first char gives a "half"
#      form, and 'k' completes it (e.g. [k=kha, Hk=bha, /k=dha, 'k=sha).
#      For simple consonants (d=ka, x=ga, t=ja, etc.) the char directly
#      gives the full consonant.
#   2. 'f' = pre-base i-matra (visually before its consonant, must be
#      reordered to after it in Unicode logical order).
#   3. 'Z' = repha (ra+halant placed on the following syllable).
#   4. Combined vowel signs: k+s = o-matra, k+S = au-matra.
#
# Mapping derived from boilerplate ground truth in Uttarakhand 2003 PDFs:
#   fuokZpd = nirvachak, mRrjkapy = uttaranchal, Hkkx = bhag,
#   la[;k = sankhya, uke = naam, flag = singh, etc.

# Multi-char compounds must be matched BEFORE single chars.
# Order: longest match first.
_KD_COMPOUNDS = [
    ('[k', 'ख'),
    ('Hk', 'भ'),
    ('/k', 'ध'),
    ('?k', 'घ'),
    ('.k', 'ण'),
    ('Fk', 'थ'),
    ("'k", 'श'),
    ('"k', 'ष'),
    ('{k', 'क्ष'),
    ('bZ', 'ई'),    # independent long i vowel
    ('Ù', 'त्त'),   # tta conjunct (rare)
]

# Single-char mapping table.
_KD_MAP = {
    # Vowels (independent)
    'v': 'अ', 'm': 'उ', 'b': 'इ',
    # Consonants (full forms - single char)
    'd': 'क', 'x': 'ग', 'p': 'च', 'N': 'छ', 't': 'ज', 'V': 'ट',
    'B': 'ठ', 'M': 'ड', 'r': 'त', 'n': 'द', 'u': 'न', 'i': 'प',
    'Q': 'फ', 'c': 'ब', 'e': 'म', 'j': 'र', 'y': 'ल', 'o': 'व',
    'l': 'स', 'g': 'ह',
    # Consonants - half forms (consonant + implicit halant)
    'T': 'ज्', 'R': 'त्', 'U': 'न्', 'E': 'म्', 'I': 'प्',
    'C': 'ब्', 'J': 'श्र', 'Y': 'ल्',
    'K': 'ज्ञ',
    # Vowel matras
    'k': 'ा',       # aa matra
    'f': '\ue010',   # pre-base i-matra (sentinel, reordered later)
    'h': 'ी',       # ii matra
    'q': 'ु',       # u matra
    'w': 'ू',       # uu matra
    's': 'े',       # e matra
    'S': 'ै',       # ai matra
    # Modifiers
    'a': 'ं',       # anusvara
    '%': 'ँ',       # chandrabindu
    'Z': '\ue011',   # repha sentinel (ra+halant, reordered later)
    '~': '्',       # halant/virama
    'z': '्र',      # subscript ra (ra-halant below)
    '+': '\u093C',   # nukta
    '=': 'त्र',     # tra conjunct
    ':': 'रु',      # ru ligature
    ';': 'य',
    # Special/extended chars
    '\u00A8': 'ो',   # o matra (standalone glyph)
    '\u00E6': 'द्र', # dra conjunct
    '\u00D2': 'भ',   # alternate bha
    '\u00E7': 'प्र', # pra conjunct
    '\u00D8': 'क्र', # kra conjunct
    '\u00C3': 'ई',   # independent ii vowel
    '\u00A1': 'ँ',   # chandrabindu (alternate)
    '\u00C0': '',     # filler/placeholder (appears standalone, no content)
    '\u00BF': '',     # filler/placeholder
    '\u00B5': '',     # page decoration (mu symbol area)
    '\u00BC': '(',    # opening bracket
    '\u00BD': ')',    # closing bracket
    '\u00A3': 'ं',   # alternate anusvara
    '\u00A9': 'ौ',   # au matra (standalone glyph)
    '\u00AA': 'ट्र', # tra (retroflex) conjunct
    '\u00B4': 'ण',   # alternate ण (used in ष्ण conjuncts like कृष्ण)
    '#': 'रु',       # alternate ru ligature
    '$': '+',        # literal plus sign
    '`': 'ृ',       # ri matra
    '\\': '/',       # slash (appears in EPIC numbers as UP\1\...)
    '&': '-',        # dash/separator
    # Digits map to themselves
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4',
    '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
    # Punctuation that passes through
    ' ': ' ', '-': '-', '.': '.', '/': '/', ',': ',',
    '(': '(', ')': ')', '[': '[', ']': ']',
    '<': 'ढ', '>': 'झ',
    '{': 'क्ष्',  # half ksha (full form via {k compound)
    '}': 'द्व',
    '|': 'प्र',
    '@': 'ऋ',
    '^': '?',  # unmapped
    '*': '?',  # unmapped
    '!': '!',
    '_': '?',  # unmapped
    'W': 'ज़',  # za with nukta (rare in data, appears in EPIC numbers)
    'P': 'P',  # appears only in EPIC numbers
    'L': 'स्',  # half sa
    'G': 'ग्',  # half ga (rare)
    'H': 'भ्',  # first char of Hk compound; standalone = half bha
    'D': 'क्',  # half ka (rare)
    'F': 'थ्',  # first char of Fk compound; standalone = half tha
    '?': 'घ्',  # first char of ?k compound; standalone = half gha
    '.': '.',
    "'": 'श्',  # first char of 'k compound; standalone = half sha
    '"': 'ष्',  # first char of "k compound; standalone = half sha
    '[': 'ख्',  # first char of [k compound; standalone = half kha
}

# Precompiled regex helpers for post-decode reordering.
_CONS = '[\u0915-\u0939\u0958-\u0961]'  # Devanagari consonant range
_VIRAMA = '\u094D'    # halant
_CLUSTER = '(?:' + _CONS + _VIRAMA + ')*' + _CONS
_I_SENT = '\ue010'    # pre-base i-matra sentinel
_R_SENT = '\ue011'    # repha sentinel


def _kd_decode(s):
    """Transcode a Kruti Dev 010 encoded string to Unicode Devanagari."""
    # Phase 1: multi-char compound substitution (longest match first)
    out = []
    i = 0
    n = len(s)
    while i < n:
        matched = False
        # Try two-char compounds first
        if i + 1 < n:
            pair = s[i:i+2]
            for kd, uni in _KD_COMPOUNDS:
                if pair == kd:
                    out.append(uni)
                    i += 2
                    matched = True
                    break
        if not matched:
            ch = s[i]
            if ch in _KD_MAP:
                out.append(_KD_MAP[ch])
            else:
                # Unknown character - pass through if ASCII printable, else placeholder
                if ch.isascii() and ch.isprintable():
                    out.append(ch)
                else:
                    out.append('\uFFFD')
            i += 1

    t = ''.join(out)

    # Phase 2: combine aa-matra + e-matra -> o-matra; aa + ai -> au
    t = t.replace('ाे', 'ो')
    t = t.replace('ाै', 'ौ')

    # Phase 3: combine independent a + aa-matra -> aa, etc.
    t = t.replace('अा', 'आ')
    t = t.replace('अो', 'ओ')
    t = t.replace('अौ', 'औ')
    t = t.replace('अे', 'ए')
    t = t.replace('अै', 'ऐ')

    # Phase 4: reorder pre-base i-matra (sentinel -> after following consonant cluster)
    # The sentinel appears BEFORE its consonant in visual order; move it after.
    # Also handle case where anusvara/chandrabindu sits between sentinel and consonant
    # (rare variant: 'falg' instead of standard 'flag' for सिंह)
    _NASALS = '[\u0902\u0901]'  # anusvara, chandrabindu
    t = re.sub(_I_SENT + '(' + _NASALS + ')(' + _CLUSTER + ')', r'\2' + 'ि' + r'\1', t)
    t = re.sub(_I_SENT + '(' + _CLUSTER + ')', r'\1' + 'ि', t)
    # If sentinel couldn't attach (e.g. at end), just emit ि
    t = t.replace(_I_SENT, 'ि')

    # Phase 5: reorder repha (sentinel -> before preceding consonant cluster as र्)
    # Repha appears AFTER its cluster in visual order; in logical order it's र् before.
    # Pattern: consonant-cluster + (optional matras) + repha-sentinel
    _MATRAS = '[\u093E-\u094C\u0902\u0903\u0901\u0943\u0962\u0963]'
    t = re.sub('(' + _CLUSTER + ')(' + _MATRAS + '*)' + _R_SENT,
               'र्' + r'\1\2', t)
    # If repha couldn't attach, just emit र्
    t = t.replace(_R_SENT, 'र्')

    # Phase 6: clean up stray double matras and normalize
    t = t.replace('ाा', 'ा')  # double aa -> single
    t = unicodedata.normalize('NFC', t)
    return t


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

    Kruti Dev text is non-ASCII after decoding, so "alphabetic" checks must
    look for absence of digits rather than ASCII letters.

    Fixes applied:
    - House_no with NO digits at all -> name fragment, not house number
    - Trailing non-digit text after digit-based house number -> name fragment
    """
    house = row[_HOUSE].strip()
    name = row[_NAME].strip()
    rel = row[_REL].strip()

    # House_no with NO digits at all -> it's a name fragment
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
            # Decode Kruti Dev 010 text to Unicode Devanagari
            # Skip serial (idx 0), age (idx 6), and EPIC (idx 7)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _kd_decode(cells[idx])
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


# -- CSV post-processing ---------------------------------------------------

VALID_SEX = {"पु-", "म-", ""}
VALID_RELATION = {"प-", "पि-", "मा-", "अ-", "", "0 प-", "0 पि-"}


def _postprocess(csv_path):
    """Validate sex and relation columns in an already-extracted CSV.

    Uttarakhand-specific valid values:
      sex: पु-, म-
      relation: प-, पि-, मा-, अ-, empty, "0 प-", "0 पि-"
    Any value outside these sets is cleared to empty.
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
                    row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                if rel not in VALID_RELATION:
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


# -- Main ------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Extract Uttarakhand SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/uttarakhand")
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
