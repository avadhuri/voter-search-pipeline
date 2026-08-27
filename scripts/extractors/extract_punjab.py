"""
Extract Punjab old SIR (2003) voter roll PDFs to CSV.

PDF uses PN-TTAmarEN legacy Gurmukhi font. Text extracted as raw 8-bit
bytes that only render correctly through the embedded font. This module
includes a byte-to-Unicode Gurmukhi transcoder (``_pntt_decode``) built
by matching boilerplate text against known Punjabi words.

The only reordering needed is for sihari (U+0A3F): the font stores it
BEFORE the consonant (visual order), but Unicode expects it after.

Usage:
    python scripts/extractors/extract_punjab.py                    # all downloaded ACs
    python scripts/extractors/extract_punjab.py --ac 1,2,3         # specific ACs
    python scripts/extractors/extract_punjab.py --limit 5          # first 5 ACs
    python scripts/extractors/extract_punjab.py --combined         # single CSV for state
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


# ---------------------------------------------------------------------------
# PN-TTAmarEN -> Unicode Gurmukhi transcoder
# ---------------------------------------------------------------------------
#
# HOW THE TABLE WAS DERIVED
# -------------------------
# The PN-TTAmarEN font has no /ToUnicode CMap. The mapping was built by
# extracting boilerplate text whose correct Gurmukhi is known and matching
# byte-by-byte:
#
#   1. PAGE TITLE: "m¨Sj nyMv a|Osd 2003" = "ਵੋਟਰ ਸੂਚੀ ਪੰਜਾਬ 2003"
#   2. FOOTER LEGEND (every data page):
#      Col 4: (ua.-uaYs, g.-gsYs, a.-aYv) = (ਪਿ.-ਪਿਤਾ, ਮ.-ਮਾਤਾ, ਪ.-ਪਤੀ)
#      Col 6: (ax.-axjo, uB.-uBnYjv)     = (ਪੁ.-ਪੁਰਸ਼, ਇ.-ਇਸਤਰੀ)
#   3. AC NAMES cross-referenced against the portal's English names:
#      aTs`E¨S = ਪਠਾਨਕੋਟ (PATHANKOT), buYpI[- = ਫਤਿਹਗੜ੍ਹ (FATEHGARH),
#      Ixj]snaxj = ਗੁਰਦਾਸਪੁਰ (GURDASPUR), etc.
#   4. HIGH-FREQUENCY NAMES validated against Punjabi name databases:
#      un|K = ਸਿੰਘ (Singh), E¬j = ਕੌਰ (Kaur), EjYsj = ਕਰਤਾਰ (Kartar),
#      g`OvY = ਮਨਜੀਤ (Manjit), Ixj]snaxj = ਗੁਰਦਾਸਪੁਰ (Gurdaspur), etc.
#
# ENCODING PROPERTIES
# -------------------
# - Direct substitution: each byte maps to one or more Unicode code points.
# - Sihari (ਿ U+0A3F) is stored BEFORE its consonant (visual order).
#   Post-processing reorders it to Unicode logical order.
# - Independent vowel ਇ is encoded as ਿ (sihari, 'u') + ਇ-holder ('B').
#   Post-processing collapses ਿ+ਇ -> ਇ.
# - Multiple font bytes may map to the same Gurmukhi char (glyph variants
#   for different matra combinations, common in legacy Indic fonts).
#
# UNMAPPED BYTES
# --------------
# A handful of rare bytes (w, *, etc.) could not be confidently identified
# and are left out of _PNTT_MAP. _pntt_decode() replaces them with U+FFFD.
# Measured on AC001 (121k rows): 0.072% of rows contain an unknown glyph.

_PNTT_MAP = {
    # --- Consonants ---
    'E': 'ਕ',   # ka
    'F': 'ਕ',   # ka (sihari-context glyph variant)
    'G': 'ਖ',   # kha
    'I': 'ਗ',   # ga
    'K': 'ਘ',   # gha
    'M': 'ਚ',   # cha
    'N': 'ਛ',   # chha
    'O': 'ਜ',   # ja
    'P': 'ਜ਼',  # za (ja + nukta)
    'Q': 'ਝ',   # jha
    'S': 'ਟ',   # tta
    'T': 'ਠ',   # ttha
    'U': 'ਡ',   # dda
    'X': 'ਣ',   # nna
    'Y': 'ਤ',   # ta
    '\\': 'ਥ',  # tha
    ']': 'ਦ',   # da
    '_': 'ਧ',   # dha
    '`': 'ਨ',   # na
    'a': 'ਪ',   # pa
    'b': 'ਫ',   # pha
    'd': 'ਬ',   # ba
    'e': 'ਭ',   # bha
    'g': 'ਮ',   # ma
    'h': 'ਯ',   # ya
    'j': 'ਰ',   # ra
    'k': 'ਲ',   # la
    'm': 'ਵ',   # va
    'n': 'ਸ',   # sa
    'o': 'ਸ਼',  # sha (sa + nukta form)
    'p': 'ਹ',   # ha
    '[': 'ੜ',   # rra (hard rha)

    # --- Independent vowels ---
    'A': 'ਅ',   # a
    'B': 'ਇ',   # i (also acts as sihari holder: ਿ+ਇ -> ਇ)
    'C': 'ਉ',   # u
    'D': 'ਓ',   # o

    # --- Dependent vowel signs (matras) ---
    's': 'ਾ',   # kanna (aa)
    'u': 'ਿ',   # sihari (i) -- stored BEFORE consonant, reordered below
    'v': 'ੀ',   # bihari (ii)
    'x': 'ੁ',   # aunkar (u)
    'z': 'ੁ',   # aunkar variant
    'y': 'ੂ',   # dulainkar (uu)
    '~': 'ੇ',   # lavan (e)
    '\xa1': 'ੇ',  # lavan variant
    '\xa4': 'ੈ',  # dulavan (ai)
    '\xa5': 'ੈ',  # dulavan variant
    '\xa2': 'ੈ',  # dulavan variant
    '\xa8': 'ੋ',  # hohra (o)
    '\xa9': 'ੋ',  # hohra variant
    '\xac': 'ੌ',  # kanaura (au)

    # --- Other signs ---
    '|': 'ੰ',   # tippi (nasal)
    '\xa6': 'ੰ',  # tippi variant
    '\xb0': 'ੱ',  # adhak (gemination)
    '\xb1': 'ੱ',  # adhak variant
    'q': 'ੱ',   # adhak variant
    '\xae': '\u0A4D\u0A30',  # subjoined ra (halant + ra, for pr- conjuncts)

    # --- Miscellaneous / rare ---
    '}': 'ੈ',   # dulavan variant (appears in boilerplate `}:, `}.)
    't': 'ਾਂ',  # kanna + bindi (plural marker in boilerplate)
    'r': 'ੰ',   # tippi variant (appears in ਉੰਕਾਰ = Onkaar)
    '#': 'ੰ',   # tippi variant
    '\xaf': 'ਵ',  # va variant
    'W': 'ਢ',   # ddha
    'Z': 'ਞ',   # nyanya (rare; also appears in ZZZ null-placeholder)
    '\u2212': '੍ਹ',  # en-dash used as halant+ha (in ੜ੍ਹ sequences)

    # --- Pass-through ---
    ' ': ' ', '.': '.', ',': ',', ':': ':', ';': ';', '-': '-',
    '(': '(', ')': ')', '/': '/', '!': '!', '?': '?', "'": "'",
    **{c: c for c in '0123456789'},
}

# Gurmukhi consonant range for reordering regex
_GCONS = '[\u0A15-\u0A39\u0A59-\u0A5E]'  # ਕ-ਹ plus ਖ਼ etc
_GSIHARI = '\u0A3F'  # ਿ


def _pntt_decode(s):
    """Transcode a PN-TTAmarEN string to Unicode Gurmukhi.

    Returns the decoded string. Unknown bytes are replaced with U+FFFD.
    """
    out = []
    for ch in s:
        if ch in _PNTT_MAP:
            out.append(_PNTT_MAP[ch])
        elif ch.isascii() and (ch.isdigit() or ch in '()[]{}/.,:;-'):
            out.append(ch)
        else:
            out.append('\ufffd')
    t = ''.join(out)

    # --- Sihari reordering ---
    # The font stores sihari BEFORE its consonant (visual order).
    # Unicode wants it AFTER. Move ਿ past the following consonant.
    t = re.sub(_GSIHARI + '(' + _GCONS + ')', r'\1' + _GSIHARI, t)

    # --- Collapse ਿ + ਇ -> ਇ ---
    # When sihari has no preceding consonant (word-initial or after space),
    # the font encodes independent ਇ as sihari + ਇ-holder.
    t = t.replace(_GSIHARI + '\u0A07', '\u0A07')  # ਿ+ਇ -> ਇ

    # --- Combine independent vowel parts ---
    # ਇ + ੀ -> ਈ (ii from i-base + bihari)
    t = t.replace('\u0A07\u0A40', '\u0A08')
    # ਉ + ੁ -> ਉ (the font sometimes emits Cx = ਉ+ੁ; collapse to just ਉ)
    t = t.replace('\u0A09\u0A41', '\u0A09')
    # ਉ + ੂ -> ਊ (uu from u-base + dulainkar)
    t = t.replace('\u0A09\u0A42', '\u0A0A')
    # ਅ + ਾ -> ਆ
    t = t.replace('\u0A05\u0A3E', '\u0A06')
    # ਅ + ੇ -> ਏ
    t = t.replace('\u0A05\u0A47', '\u0A0F')
    # ਅ + ੈ -> ਐ
    t = t.replace('\u0A05\u0A48', '\u0A10')
    # ਓ + ੌ -> ਔ  (or ਅ + ੌ -> ਔ)
    t = t.replace('\u0A13\u0A4C', '\u0A14')
    t = t.replace('\u0A05\u0A4C', '\u0A14')

    return unicodedata.normalize('NFC', t)


# ---------------------------------------------------------------------------
# Relation / sex code normalization
# ---------------------------------------------------------------------------
# After decoding, the relation column contains Gurmukhi abbreviations.
# Normalize to single-letter codes the rest of the pipeline expects.

_REL_MAP = {
    'ਪਿ': 'F',   # ਪਿਤਾ (Pita = Father)
    'ਪਿ.': 'F',
    'ਮ': 'M',    # ਮਾਤਾ (Mata = Mother)
    'ਮ.': 'M',
    'ਪ': 'H',    # ਪਤੀ (Pati = Husband)
    'ਪ.': 'H',
}

_SEX_MAP = {
    'ਪੁ': 'M',   # ਪੁਰਸ਼ (Purash = Male)
    'ਪੁ.': 'M',
    'ਇ': 'F',    # ਇਸਤਰੀ (Istri = Female)
    'ਇ.': 'F',
    'ਇਸ': 'F',
    'ਇਸ.': 'F',
}

STATE_ID = "punjab"
ROLL_YEAR = 2003
N_COLS = 8
NARROW_COLS = {4, 6, 7}
FALLBACK_CENTRES = {1: 56, 2: 80, 3: 171, 4: 281, 5: 320, 6: 419, 7: 472, 8: 500}
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
    # Try "1." "2." ... format
    dotted = [w for w in words if re.fullmatch(r"\d+\.", w["text"])]
    if len(dotted) >= min_cols:
        dotted.sort(key=lambda w: w["top"])
        for i in range(len(dotted)):
            group = [dotted[i]]
            for j in range(i + 1, len(dotted)):
                if abs(dotted[j]["top"] - dotted[i]["top"]) <= ROW_TOL:
                    group.append(dotted[j])
            if len(group) >= min_cols:
                labels = sorted(group, key=lambda w: w["x0"])
                nums = [int(w["text"].rstrip('.')) for w in labels]
                if nums[0] == 1 and nums[1] == 2:
                    return {n: (w["x0"] + w["x1"]) / 2 for n, w in zip(nums, labels)}
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
    - House_no with NO digits at all -> it's a name fragment, not a house number
    - Trailing non-digit text after a digit-based house number -> name fragment
    - Relation code stuck at end of elector_name moved to relation

    Uses digit-absence checks (not .isalpha()) so non-ASCII Gurmukhi text
    is handled correctly after decoding.
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
            # Decode PN-TTAmarEN to Unicode Gurmukhi (skip serial=0, age=6, epic=7)
            for idx in (_HOUSE, _NAME, _REL, 4, 5):
                cells[idx] = _pntt_decode(cells[idx])
            # Normalize relation and sex codes to single letters
            rel_raw = cells[_REL].strip().rstrip('.')
            if rel_raw in _REL_MAP:
                cells[_REL] = _REL_MAP[rel_raw]
            elif rel_raw + '.' in _REL_MAP:
                cells[_REL] = _REL_MAP[rel_raw + '.']
            sex_raw = cells[5].strip().rstrip('.')
            if sex_raw in _SEX_MAP:
                cells[5] = _SEX_MAP[sex_raw]
            elif sex_raw + '.' in _SEX_MAP:
                cells[5] = _SEX_MAP[sex_raw + '.']
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

VALID_SEX = {"M", "F", ""}
VALID_RELATION = {"F", "H", "M", "O", ""}


def _transliterate_gurmukhi(text):
    """Transliterate Gurmukhi text to approximate English via IAST."""
    if not text or not text.strip():
        return ''
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate

    # Pre-process: strip nukta (U+0A3C) — indic_transliteration doesn't
    # handle it, so ਸ਼ (sa + nukta) passes through raw.  Instead, handle
    # the common nukta-conjuncts explicitly before transliterating.
    t = text.strip()
    t = t.replace('\u0A36', '\u0A38')   # ਸ਼ (single-char sha) -> ਸ (handle below)
    t = t.replace('\u0A38\u0A3C', 'SH_PLACEHOLDER')  # ਸ਼ (sa+nukta) -> placeholder
    t = t.replace('\u0A16\u0A3C', 'KH_PLACEHOLDER')  # ਖ਼ -> placeholder
    t = t.replace('\u0A17\u0A3C', 'GH_PLACEHOLDER')  # ਗ਼ -> placeholder
    t = t.replace('\u0A1C\u0A3C', 'Z_PLACEHOLDER')   # ਜ਼ -> placeholder
    t = t.replace('\u0A2B\u0A3C', 'F_PLACEHOLDER')   # ਫ਼ -> placeholder
    t = t.replace('\u0A32\u0A3C', 'LL_PLACEHOLDER')   # ਲ਼ -> placeholder
    t = t.replace('\u0A3C', '')          # strip any remaining nukta

    iast = transliterate(t, sanscript.GURMUKHI, sanscript.IAST)

    # Restore placeholders
    iast = iast.replace('SH_PLACEHOLDER', 'sh')
    iast = iast.replace('KH_PLACEHOLDER', 'kh')
    iast = iast.replace('GH_PLACEHOLDER', 'gh')
    iast = iast.replace('Z_PLACEHOLDER', 'z')
    iast = iast.replace('F_PLACEHOLDER', 'f')
    iast = iast.replace('LL_PLACEHOLDER', 'll')

    # Contextual nasal: ṃ before a consonant should be 'n', not 'm'
    iast = re.sub(r'ṃ(?=[gkctdpbGKCTDPB])', 'n', iast)

    diacritic_map = {
        'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
        'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
        'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm',
    }
    for k, v in diacritic_map.items():
        iast = iast.replace(k, v)
    # Gurmukhi: do NOT apply aggressive schwa deletion
    # Punjabi keeps more vowels than Hindi
    # Only drop final 'a' if word is 3+ chars
    words = iast.split()
    result = []
    for w in words:
        if len(w) > 2 and w.endswith('a') and not w.endswith('aa'):
            w = w[:-1]
        result.append(w)
    return ' '.join(result).title()


_SEX_EN = {'M': 'M', 'F': 'F'}
_REL_EN = {'F': 'Father', 'H': 'Husband', 'M': 'Mother', 'O': 'Other'}


def _transliterate_csv(csv_path):
    """Add English transliteration columns to an existing Punjab CSV."""
    import tempfile, shutil

    with open(csv_path, encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        old_fields = list(reader.fieldnames)
        en_cols = ['elector_name_en', 'relation_name_en',
                   'relation_type_en', 'sex_en']
        base_fields = [f for f in old_fields if f not in en_cols]
        new_fields = base_fields + en_cols
        tmp = tempfile.NamedTemporaryFile(mode='w', newline='', encoding='utf-8',
                                          dir=os.path.dirname(csv_path),
                                          suffix='.csv', delete=False)
        try:
            writer = csv.DictWriter(tmp, fieldnames=new_fields)
            writer.writeheader()
            count = 0
            for row in reader:
                row['elector_name_en'] = _transliterate_gurmukhi(row.get('elector_name', ''))
                row['relation_name_en'] = _transliterate_gurmukhi(row.get('relation_name', ''))
                row['relation_type_en'] = _REL_EN.get(row.get('relation', '').strip(), '')
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


def _postprocess(csv_path):
    """Fix column-bleeding issues in an already-extracted CSV.

    Punjab-specific fixes:
    1. Sex column has CID-based font garbage ("(��ਬ:68)...") or Gurmukhi
       garbage → clear to empty.
    2. Sex column has name suffix + ਪੁ. ("ਸਿੰਘਪੁ.") → extract ਪੁ. is not
       applicable here since Punjab uses English F/M for sex. Just clear.
    3. Relation column has CID garbage or Gurmukhi name + valid suffix
       ("ਸਿੰਘਪਿ.", "ਕੌਰਪ.") — these are PN-TTAmar decode artifacts.
       If it ends with a known suffix (ਪਿ., ਪ.), try to extract; else clear.
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
                    # Check if it ends with a valid code after Gurmukhi prefix
                    cleaned = False
                    for valid in ("F", "H", "M", "O"):
                        if rel.endswith(" " + valid):
                            prefix = rel[:-2].strip()
                            if prefix:
                                row["elector_name"] = (row["elector_name"].strip() + " " + prefix).strip()
                            row["relation"] = valid
                            cleaned = True
                            break
                    if not cleaned:
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
    ap = argparse.ArgumentParser(description="Extract Punjab SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/punjab")
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
