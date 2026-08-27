"""
Extract Rajasthan old SIR (2002) voter roll PDFs to CSV.

PDFs use two fonts:
  - DevLys010: a legacy 8-bit Hindi font (same encoding as Kruti Dev 010)
    used for voter names, relation names, and locality text.
  - ArialUnicodeMS: used for relation types (वपतख/पवत) and sex (पयरष/सध),
    but with a broken cmap that produces garbled Unicode.

The extractor:
  1. Uses pdfplumber word extraction (not table extraction) with column
     boundaries derived from (1)(2)...(8) header markers.
  2. Transcodes DevLys010 text to proper Unicode Devanagari.
  3. Maps garbled ArialUnicodeMS relation/sex values to correct forms.

Usage:
    python scripts/extractors/extract_rajasthan.py                    # all ACs
    python scripts/extractors/extract_rajasthan.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_rajasthan.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_rajasthan.py --combined         # single CSV
    python scripts/extractors/extract_rajasthan.py --postprocess FILE # fix existing CSV
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

STATE_ID = "rajasthan"
ROLL_YEAR = 2002
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


# -- DevLys010 -> Unicode Devanagari transcoder -------------------------------
#
# DevLys010 is a legacy 8-bit Hindi font that uses the same encoding as
# Kruti Dev 010. ASCII and Latin-1 byte values map to Devanagari glyphs.
#
# Key structural properties unique to DevLys (vs standard Kruti Dev):
#   1. UPPERCASE consonants followed by 'k' form TWO-CHAR COMPOUNDS that
#      produce the base consonant WITHOUT aa-matra. The 'k' completes the
#      glyph shape but does NOT emit ा (aa-matra). E.g., Ek = म, Tk = ज.
#      This is the critical difference from standard Kruti Dev decoders.
#   2. Standard Kruti Dev compounds also apply: [k=ख, Hk=भ, /k=ध, 'k=श, etc.
#   3. 'f' = pre-base i-matra (reordered to after its consonant in Unicode).
#   4. 'Z' = repha (ra+halant placed on the following syllable).
#   5. Combined vowel signs: ा + े = ो (o-matra), ा + ै = ौ (au-matra).

# Multi-char compounds: longest match first, checked before single chars.
# DevLys uppercase+k compounds come first (these eat the 'k' that would
# otherwise become aa-matra).
_DL_COMPOUNDS = [
    # Uppercase consonant + k = base consonant (DevLys-specific)
    ('Ek', 'म'),   ('Tk', 'ज'),   ('Lk', 'स'),   ('Ok', 'व'),
    ('Xk', 'ग'),   ('Rk', 'त'),   ('Uk', 'न'),   ('Ik', 'प'),
    ('Ck', 'ब'),   ('Mk', 'ड'),   ('Nk', 'छ'),   ('Vk', 'ट'),
    ('Bk', 'ठ'),   ('Dk', 'क'),   ('Jk', 'श्र'),  ('Kk', 'ज्ञ'),
    ('Yk', 'ल'),   ('Qk', 'फ'),   ('Pk', 'च'),
    # Standard Kruti Dev two-char compounds
    ('[k', 'ख'),   ('Hk', 'भ'),   ('/k', 'ध'),   ('?k', 'घ'),
    ('.k', 'ण'),   ('Fk', 'थ'),   ("'k", 'श'),   ('"k', 'ष'),
    ('{k', 'क्ष'),
    ('bZ', 'ई'),   # independent long i vowel
    ('Ù', 'त्त'),  # tta conjunct
]

# Single-char mapping table.
_DL_MAP = {
    # Vowels (independent)
    'v': 'अ', 'm': 'उ', 'b': 'इ',
    # Consonants (full forms)
    'd': 'क', 'x': 'ग', 'p': 'च', 'N': 'छ', 't': 'ज', 'V': 'ट',
    'B': 'ठ', 'M': 'ड', 'r': 'त', 'n': 'द', 'u': 'न', 'i': 'प',
    'Q': 'फ', 'c': 'ब', 'e': 'म', 'j': 'र', 'y': 'ल', 'o': 'व',
    'l': 'स', 'g': 'ह',
    # Uppercase consonants (standalone, without following 'k') = half forms
    'T': 'ज्', 'R': 'त्', 'U': 'न्', 'E': 'म्', 'I': 'प्',
    'C': 'ब्', 'J': 'श्र', 'Y': 'ल्', 'L': 'स्', 'O': 'व्',
    'X': 'ग्', 'P': 'च्', 'K': 'ज्ञ',
    'D': 'क्', 'G': 'ग्', 'H': 'भ्', 'F': 'थ्',
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
    'z': '्र',      # subscript ra
    '+': '\u093C',   # nukta
    '=': 'त्र',     # tra conjunct
    ':': 'रु',      # ru ligature
    ';': 'य',
    '#': 'रु',      # alternate ru ligature
    '`': 'ृ',       # ri matra
    '\\': '/',      # slash
    '&': '-',       # dash/separator
    '$': '+',       # literal plus
    '<': 'ढ',
    '>': 'झ',
    '{': 'क्ष्',
    '}': 'द्व',
    '|': 'प्र',
    '@': 'ऋ',
    'W': 'ज़',
    # Special/extended chars
    '\u00A8': 'ो',   # o matra (standalone glyph)
    '\u00E6': 'द्र', # dra conjunct (æ)
    '\u00C7': 'प्र', # pra conjunct (Ç)
    '\u00AB': 'त्र', # tra conjunct («)
    '\u00B8': 'य',   # ya (¸) - used in DevLys for य
    '\u2122': 'न्न', # nna double consonant (™)
    '\u00D2': 'भ',   # alternate bha
    '\u00D8': 'क्र', # kra conjunct
    '\u00C3': 'ई',   # independent ii vowel
    '\u00A1': 'ँ',   # chandrabindu (alternate)
    '\u00C0': '',    # filler
    '\u00BF': '',    # filler
    '\u00B5': '',    # page decoration
    '\u00BC': '(',   # opening bracket
    '\u00BD': ')',   # closing bracket
    '\u00A3': 'ं',   # alternate anusvara
    '\u00A9': 'ौ',   # au matra (standalone glyph)
    '\u00AA': 'ट्र', # tra (retroflex) conjunct
    '\u00B4': 'ण',   # alternate ण
    # Digits map to themselves
    '0': '0', '1': '1', '2': '2', '3': '3', '4': '4',
    '5': '5', '6': '6', '7': '7', '8': '8', '9': '9',
    # Punctuation
    ' ': ' ', '-': '-', '.': '.', '/': '/', ',': ',',
    '(': '(', ')': ')', '[': '[', ']': ']',
    '!': '!',
    "'": 'श्',   # standalone = half sha
    '"': 'ष्',   # standalone = half sha
    '[': 'ख्',   # standalone = half kha
    '?': 'घ्',   # standalone = half gha
}

# Precompiled regex helpers for post-decode reordering.
_CONS = '[\u0915-\u0939\u0958-\u0961]'
_VIRAMA = '\u094D'
_CLUSTER = '(?:' + _CONS + _VIRAMA + ')*' + _CONS
_I_SENT = '\ue010'
_R_SENT = '\ue011'
_MATRAS = '[\u093E-\u094C\u0902\u0903\u0901\u0943\u0962\u0963]'
_NASALS = '[\u0902\u0901]'

# Precompiled set of vowel matras (for virama+matra cleanup)
_VOWEL_MATRAS = set(chr(c) for c in range(0x093E, 0x094D))
_VOWEL_MATRAS.update({'\u0962', '\u0963', '\u0943'})


def _dl_decode(s):
    """Transcode a DevLys010 encoded string to Unicode Devanagari."""
    if not s:
        return s

    # Phase 1: compound substitution (longest match first)
    out = []
    i = 0
    n = len(s)
    while i < n:
        matched = False
        if i + 1 < n:
            pair = s[i:i+2]
            for kd, uni in _DL_COMPOUNDS:
                if pair == kd:
                    out.append(uni)
                    i += 2
                    matched = True
                    break
        if not matched:
            ch = s[i]
            if ch in _DL_MAP:
                out.append(_DL_MAP[ch])
            else:
                if ch.isascii() and ch.isprintable():
                    out.append(ch)
                else:
                    out.append('\uFFFD')
            i += 1

    t = ''.join(out)

    # Phase 2: remove virama before vowel matra (half-form + matra = full + matra)
    result = []
    for i, ch in enumerate(t):
        if ch == _VIRAMA and i + 1 < len(t) and t[i + 1] in _VOWEL_MATRAS:
            continue  # skip virama, matra will follow
        result.append(ch)
    t = ''.join(result)

    # Phase 3: combine aa-matra + e-matra -> o-matra; aa + ai -> au
    t = t.replace('ाे', 'ो')
    t = t.replace('ाै', 'ौ')

    # Phase 4: combine independent a + vowel sign -> full vowel
    t = t.replace('अा', 'आ')
    t = t.replace('अो', 'ओ')
    t = t.replace('अौ', 'औ')
    t = t.replace('अे', 'ए')
    t = t.replace('अै', 'ऐ')

    # Phase 5: reorder pre-base i-matra
    t = re.sub(_I_SENT + '(' + _NASALS + ')(' + _CLUSTER + ')', r'\2' + 'ि' + r'\1', t)
    t = re.sub(_I_SENT + '(' + _CLUSTER + ')', r'\1' + 'ि', t)
    t = t.replace(_I_SENT, 'ि')

    # Phase 6: reorder repha
    t = re.sub('(' + _CLUSTER + ')(' + _MATRAS + '*)' + _R_SENT,
               'र्' + r'\1\2', t)
    t = t.replace(_R_SENT, 'र्')

    # Phase 7: cleanup
    t = t.replace('ाा', 'ा')
    t = unicodedata.normalize('NFC', t)
    return t


# -- Garbled ArialUnicodeMS text mapping ------------------------------------
# The ArialUnicodeMS font in these PDFs has a broken cmap. The following
# maps the garbled Unicode strings to their correct Devanagari values.

_GARBLED_RELATION = {
    'वपतख': 'पिता',    # father
    'पवत': 'पति',      # husband
    'मखतख': 'माता',    # mother (rare)
    'अनख': 'अन्य',    # other (very rare)
}

_GARBLED_SEX = {
    'पयरष': 'पुरुष',   # male
    'सध': 'स्त्री',     # female
}

# Normalized output values for postprocessing
_RELATION_MAP = {
    'पिता': 'पि-',
    'पति': 'प-',
    'माता': 'मा-',
    'अन्य': 'अ-',
}

_SEX_MAP = {
    'पुरुष': 'पु.',
    'स्त्री': 'म.',
}


def _fix_garbled_unicode(text):
    """Fix garbled ArialUnicodeMS text by mapping known bad strings."""
    text = text.strip()
    if text in _GARBLED_RELATION:
        return _GARBLED_RELATION[text]
    if text in _GARBLED_SEX:
        return _GARBLED_SEX[text]
    return text


def _is_devlys(text):
    """Check if text is likely DevLys010 encoded (contains ASCII letters)."""
    return any(c.isascii() and c.isalpha() for c in text)


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
    """Find y of the column-number header row to skip."""
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
    """Post-process a row to fix common column-assignment errors."""
    house = row[_HOUSE].strip()
    name = row[_NAME].strip()
    rel = row[_REL].strip()

    if house and not re.search(r"\d", house):
        name = f"{house} {name}".strip()
        house = ""

    if house:
        m = re.match(r"^(.*?\d[\d/\-]*)\s+(\S.*)$", house)
        if m:
            house = m.group(1).strip()
            name = f"{m.group(2).strip()} {name}".strip()

    row[_HOUSE] = house
    row[_NAME] = name
    row[_REL] = rel


def _decode_cell(text, is_name_or_rel=True):
    """Decode a cell's text: DevLys010 -> Unicode, or fix garbled Unicode."""
    if not text:
        return text
    if _is_devlys(text):
        return _dl_decode(text)
    else:
        return _fix_garbled_unicode(text)


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
            # Decode DevLys010 / fix garbled Unicode for text columns
            # Skip serial (idx 0), age (idx 6), and EPIC (idx 7)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _decode_cell(cells[idx])
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

VALID_SEX = {"पु.", "म.", ""}
VALID_RELATION = {"पि-", "प-", "मा-", "अ-", ""}


def _postprocess(csv_path):
    """Normalize sex and relation columns and validate.

    Maps full Devanagari forms to abbreviated forms:
      relation: पिता -> पि-, पति -> प-, माता -> मा-
      sex: पुरुष -> पु., स्त्री -> म.
    Any value outside valid sets is cleared to empty.
    """
    import tempfile
    import shutil

    fixes = {"sex_norm": 0, "rel_norm": 0, "sex_cleaned": 0,
             "rel_cleaned": 0, "total": 0}

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

                # Normalize full forms to abbreviated
                if sex in _SEX_MAP:
                    row["sex"] = _SEX_MAP[sex]
                    fixes["sex_norm"] += 1
                elif sex not in VALID_SEX:
                    row["sex"] = ""
                    fixes["sex_cleaned"] += 1

                if rel in _RELATION_MAP:
                    row["relation"] = _RELATION_MAP[rel]
                    fixes["rel_norm"] += 1
                elif rel not in VALID_RELATION:
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
    print(f"  {fixes['total']:,} rows")
    print(f"  {fixes['sex_norm']} sex normalized, {fixes['sex_cleaned']} sex cleaned")
    print(f"  {fixes['rel_norm']} relation normalized, {fixes['rel_cleaned']} relation cleaned")


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
    ap = argparse.ArgumentParser(description="Extract Rajasthan SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/rajasthan")
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
