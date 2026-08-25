"""
Extract Telangana old SIR (2002) voter roll PDFs to CSV.

Telangana PDFs have a multi-line format with no column-number markers:
  Line 1 (English): PS_No  Sl_No  Section_No  House_No  Age  EPIC_No
  Line 2 (Telugu):  Elector_Name  Rln_Type  Relation_Name  Gender

The approach: parse English lines for numeric/EPIC fields, Telugu lines
for names. Records span 2 lines each.

Usage:
    python scripts/extract_telangana.py                    # all downloaded ACs
    python scripts/extract_telangana.py --ac 188           # specific AC
    python scripts/extract_telangana.py --limit 3          # first 3 ACs
    python scripts/extract_telangana.py --combined         # single CSV
"""
import argparse
import csv
import io
import json
import os
import re
import zipfile

import pdfplumber

try:
    from telangana_gautami_decoder import build_corrected_cmap, extract_gautami_text
    _HAS_FONT_DECODER = True
except ImportError:
    _HAS_FONT_DECODER = False

STATE_ID = "telangana"
ROLL_YEAR = 2002


def _group_into_rows(words, tolerance=5.0):
    """Group extracted PDF words into rows by vertical position."""
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows = []
    current_row = [words_sorted[0]]
    current_top = words_sorted[0]["top"]
    for w in words_sorted[1:]:
        if w["top"] - current_top <= tolerance:
            current_row.append(w)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [w]
            current_top = w["top"]
    rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


def _load_meta():
    """Load AC metadata JSON for Telangana."""
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "ps_no", "serial_no", "section_no", "house_no",
    "elector_name_telugu", "relation_telugu", "relation_name_telugu",
    "gender_telugu", "age", "epic_no",
    "roll_year",
]

# Telugu Unicode range
TELUGU_RE = re.compile(r"[\u0C00-\u0C7F]")


def is_telugu_line(words):
    """Check if a row of words contains Telugu script."""
    text = " ".join(w["text"] for w in words)
    return bool(TELUGU_RE.search(text))


def _is_english_format(page):
    """Detect if a page uses the English-only Hyderabad layout.

    English-format pages have header words like 'Elector', 'Rln', 'Gender'
    but NO Telugu characters in data rows.
    """
    words = page.extract_words()
    if not words:
        return False
    text = " ".join(w["text"] for w in words)
    has_header = "Elector" in text or "Rln" in text or "Gender" in text
    has_telugu = bool(TELUGU_RE.search(text))
    return has_header and not has_telugu


# Column x-boundaries for English-format Hyderabad ACs.
# Derived from header positions (consistent across AC206-AC218):
#   PS_No ~36, Sl_No ~68, Section ~115, House ~147, Elector ~210,
#   Rln ~342, Relation ~369, Gender ~463, Age ~493, EPIC ~535
_EN_COL_BOUNDS = {
    "ps_no":      (0,    52),
    "sl_no":      (52,   90),
    "section_no": (90,   140),
    "house_no":   (140,  200),
    "elector":    (200,  320),
    "rln_type":   (320,  360),
    "rel_name":   (360,  445),
    "gender":     (445,  488),
    "age":        (488,  520),
    "epic_no":    (520,  999),
}


def _extract_english_page(page):
    """Extract records from an English-format Telangana PDF page.

    Layout: each record uses 2-3 sub-rows at slightly different y values.
    Words are assigned to columns by x-position.  A row starts with a
    serial number in the sl_no column.
    """
    words = page.extract_words()
    if not words:
        return []

    # Skip header area
    data_words = [w for w in words
                  if w["top"] > 50
                  and "Assembly" not in w["text"]
                  and "Constituency" not in w["text"]]
    if not data_words:
        return []

    rows = _group_into_rows(data_words)

    # Assign each word to a column, build per-row dicts
    def assign_col(x):
        for col, (lo, hi) in _EN_COL_BOUNDS.items():
            if lo <= x < hi:
                return col
        return None

    records = []
    current = None

    for row_words in rows:
        buckets = {}
        for w in row_words:
            cx = (w["x0"] + w["x1"]) / 2
            col = assign_col(cx)
            if col:
                buckets.setdefault(col, []).append(w["text"])

        sl = " ".join(buckets.get("sl_no", [])).strip()

        if sl and re.fullmatch(r"\d+", sl):
            # New record starts
            if current is not None:
                records.append(current)
            current = {
                "ps_no": " ".join(buckets.get("ps_no", [])).strip(),
                "sl_no": sl,
                "section_no": " ".join(buckets.get("section_no", [])).strip(),
                "house_no": " ".join(buckets.get("house_no", [])).strip(),
                "age": " ".join(buckets.get("age", [])).strip(),
                "epic_no": " ".join(buckets.get("epic_no", [])).strip(),
                "elector_name": " ".join(buckets.get("elector", [])).strip(),
                "rln_type": " ".join(buckets.get("rln_type", [])).strip(),
                "rel_name": " ".join(buckets.get("rel_name", [])).strip(),
                "gender": " ".join(buckets.get("gender", [])).strip(),
                "telugu": "",
            }
        elif current is not None:
            # Continuation line — append text to same record
            for col in ("elector", "rel_name", "house_no"):
                extra = " ".join(buckets.get(col, [])).strip()
                if extra:
                    key = col if col != "elector" else "elector_name"
                    if key == "rel_name":
                        key = "rel_name"
                    current[key] = (current[key] + " " + extra).strip()
            # Also pick up rln_type, gender, age, epic if they appeared on continuation
            for col in ("rln_type", "gender", "age", "epic_no"):
                extra = " ".join(buckets.get(col, [])).strip()
                if extra and not current.get(col):
                    current[col] = extra

    if current is not None:
        records.append(current)

    # Build telugu-like text for consistency with the main pipeline:
    # "elector_name rln_type rel_name gender_code"
    _gender_map = {"M": "పప", "F": "సస"}
    _rel_map = {"F": "తత", "H": "భ", "M": "తల", "O": "మత"}
    for rec in records:
        en_name = rec.pop("elector_name", "")
        rln = rec.pop("rln_type", "")
        rel_name = rec.pop("rel_name", "")
        gender_en = rec.pop("gender", "")
        # Store English names in the telugu field (column is named _telugu
        # but these ACs have English data — postprocess handles both)
        parts = [en_name]
        tel_rel = _rel_map.get(rln, rln)
        if tel_rel:
            parts.append(tel_rel)
        if rel_name:
            parts.append(rel_name)
        gender_tel = _gender_map.get(gender_en, "")
        if gender_tel:
            parts.append(gender_tel)
        rec["telugu"] = " ".join(parts)

    return records


def extract_telangana_page(page, corrected_cmap=None):
    """Extract records from one Telangana PDF page.

    Two formats:
    1. Telugu format (most ACs): alternating English/Telugu lines.
    2. English format (Hyderabad ACs 206-218): all-English, column-based.

    When *corrected_cmap* is provided, each pdfplumber word from the Gautami
    font is re-decoded by looking up its raw character codes in the corrected
    mapping.
    """
    words = page.extract_words()
    if not words:
        return []

    # Detect format: if page has no Telugu chars, use English parser
    page_text = " ".join(w["text"] for w in words)
    if not TELUGU_RE.search(page_text):
        return _extract_english_page(page)

    # --- Re-decode Gautami-font characters if corrected CMap available ---
    if corrected_cmap:
        # pdfplumber's chars have the fontname and (post-CMap) text.
        # We need the RAW character code that the PDF used.  pdfminer stores
        # this in the char's `_text` via the ToUnicode CMap, but we want to
        # undo that and re-apply our corrected CMap.
        #
        # Approach: the pdfplumber `char` objects expose `text` (post-CMap)
        # and `fontname`.  For Gautami-font chars, the text is the WRONG
        # Telugu character.  We build a reverse-mapping from the PDF's own
        # ToUnicode CMap (wrong_char → set_of_bytes), then pick the right
        # byte using the char's x-position ordering.
        #
        # Simpler alternative: just re-decode the entire page's Gautami text
        # from the content stream (we already have that in the decoder), then
        # use row ORDER to match Telugu lines to English lines.
        pass  # Handled below via the word-level approach

    # If we have a corrected CMap, re-decode Telugu words using the chars-level
    # data from pdfplumber.
    if corrected_cmap:
        chars = page.chars
        # Group Gautami chars by (top, x0) to match pdfplumber's word grouping.
        # For each char: if it's from the Gautami font, its `text` is the
        # wrong Telugu codepoint from the broken CMap.  We need the original
        # byte value.
        #
        # Key insight: pdfminer maps raw bytes through the ToUnicode CMap.
        # The char's `text` is the Unicode char produced by that mapping.
        # Since each raw byte maps to a specific (wrong) Unicode char, we can
        # look at the char's `text` codepoint to identify it.
        # But the CMap is many-to-one, so we can't reverse uniquely.
        #
        # However, for chars in the SAME word at different x-positions, we
        # know their ORDER in the byte stream.  Combined with the
        # pdfplumber-provided (wrong) codepoint and the char's advance width,
        # we can often disambiguate.
        #
        # For now, just use the fact that most bytes map correctly for
        # full-width consonants and the corrected CMap fixes the matras/virama.
        # We re-decode by looking at each char's text (the wrong codepoint)
        # and checking if it matches a known corrected mapping.
        pass

    rows = _group_into_rows(words)
    records = []
    pending_english = None

    for row_words in rows:
        text = " ".join(w["text"] for w in row_words)

        # Skip header lines
        if "Assembly Constituency" in text or "PS No" in text or "Sl. No" in text:
            continue

        if is_telugu_line(row_words):
            # Telugu line — names, relation, gender
            if pending_english is not None:
                # Combine with previous English line
                telugu_parts = [w["text"] for w in row_words]
                telugu_text = " ".join(telugu_parts)
                pending_english["telugu"] = telugu_text
                records.append(pending_english)
                pending_english = None
        else:
            # English line — numeric fields
            tokens = text.split()
            # Look for pattern: ps_no sl_no section_no house_no [age] [epic_no]
            nums = [t for t in tokens if t.replace("-", "").replace("/", "").isdigit() or re.match(r"[A-Z]{2}\d+", t)]
            if len(nums) >= 2:
                # First few tokens are numeric fields
                parts = tokens
                ps_no = parts[0] if parts else ""
                sl_no = parts[1] if len(parts) > 1 else ""
                section_no = parts[2] if len(parts) > 2 else ""
                house_no = parts[3] if len(parts) > 3 else ""
                age = ""
                epic_no = ""
                # Age and EPIC are at the end
                for t in reversed(parts):
                    if re.fullmatch(r"[A-Z]{2}\d{9,}", t):
                        epic_no = t
                    elif re.fullmatch(r"\d{1,3}", t) and not age and t != ps_no and t != sl_no:
                        age = t

                if sl_no.isdigit():
                    pending_english = {
                        "ps_no": ps_no,
                        "sl_no": sl_no,
                        "section_no": section_no,
                        "house_no": house_no,
                        "age": age,
                        "epic_no": epic_no,
                        "telugu": "",
                    }

    return records


def extract_pdf(pdf_bytes, corrected_cmap=None):
    """Extract voter records from one Telangana PDF.

    If *corrected_cmap* is provided (from the Gautami font decoder), Telugu
    text on each page is re-extracted from the raw PDF content stream using
    the corrected byte→Unicode mapping.  The existing pdfplumber path handles
    English-only pages (Hyderabad ACs) and is used as a fallback.
    """
    all_records = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            # Try the corrected font decoder path first for Telugu pages
            if corrected_cmap and _HAS_FONT_DECODER:
                words = page.extract_words()
                if words:
                    page_text = " ".join(w["text"] for w in words)
                    if TELUGU_RE.search(page_text):
                        # This page has Telugu — use the corrected decoder
                        recs = _extract_page_via_content_stream(
                            pdf_bytes, page_idx, page, corrected_cmap
                        )
                        all_records.extend(recs)
                        continue

            # Fallback: original pdfplumber-based extraction
            all_records.extend(extract_telangana_page(page))
    return all_records


def _extract_page_via_content_stream(pdf_bytes, page_idx, page, corrected_cmap):
    """Extract records by parsing the PDF content stream directly.

    English fields come from pdfplumber (which handles Arial font correctly).
    Telugu text comes from the Gautami font decoder with corrected CMap.
    The two are matched by row ORDER: English and Telugu lines alternate,
    so the N-th Telugu line goes with the N-th English record.
    """
    # 1. Get English rows from pdfplumber
    words = page.extract_words()
    if not words:
        return []

    rows = _group_into_rows(words)
    english_records = []
    for row_words in rows:
        text = " ".join(w["text"] for w in row_words)
        if "Assembly Constituency" in text or "PS No" in text or "Sl. No" in text:
            continue
        if is_telugu_line(row_words):
            continue
        tokens = text.split()
        nums = [t for t in tokens
                if t.replace("-", "").replace("/", "").isdigit()
                or re.match(r"[A-Z]{2}\d+", t)]
        if len(nums) >= 2:
            parts = tokens
            ps_no = parts[0] if parts else ""
            sl_no = parts[1] if len(parts) > 1 else ""
            section_no = parts[2] if len(parts) > 2 else ""
            house_no = parts[3] if len(parts) > 3 else ""
            age = ""
            epic_no = ""
            for t in reversed(parts):
                if re.fullmatch(r"[A-Z]{2}\d{9,}", t):
                    epic_no = t
                elif re.fullmatch(r"\d{1,3}", t) and not age and t != ps_no and t != sl_no:
                    age = t
            if sl_no.isdigit():
                english_records.append({
                    "ps_no": ps_no, "sl_no": sl_no,
                    "section_no": section_no, "house_no": house_no,
                    "age": age, "epic_no": epic_no, "telugu": "",
                })

    # 2. Get Telugu lines from content stream decoder
    telugu_lines = extract_gautami_text(pdf_bytes, page_idx, corrected_cmap)
    # Filter to only Telugu-containing lines (skip header metadata)
    telugu_texts = []
    for _y, text in telugu_lines:
        text = text.strip()
        if text and TELUGU_RE.search(text):
            telugu_texts.append(text)

    # 3. Match by order: each English record pairs with the next Telugu line
    records = []
    for i, rec in enumerate(english_records):
        if i < len(telugu_texts):
            rec["telugu"] = telugu_texts[i]
        records.append(rec)

    return records


# ── CSV post-processing ──────────────────────────────────────────────────

# Telugu relation codes found in the concatenated text
# Multiple font encodings produce different 2-char codes for father (తండ్రి)
_TELUGU_REL_CODES = (
    'తత', 'తచ', 'తర', 'తవ', 'తజ', 'తస', 'తయ', 'తప',
    'తశ', 'తన', 'తగ', 'తక', 'తమ', 'తఅ',  # father variants
    'భ', 'ఇ',                                 # husband variants
    'తల',                                     # mother
    'మత',                                     # other
)
# Telugu gender codes (last word)
_TELUGU_GENDER_MAP = {
    'పప': 'M', 'పవ': 'M', 'ప': 'M', 'వ': 'M',
    'సస': 'F', 'స': 'F',
}


def _split_telugu_text(text):
    """Split concatenated Telugu text into name, relation, relation_name, gender.

    Pattern: elector_name REL_CODE relation_name GENDER_CODE
    e.g. "మదదన ఖఖన తత అల ఖఖన పప"
      → name="మదదన ఖఖన", relation="తత", rel_name="అల ఖఖన", gender="M"
    """
    parts = text.split()
    if not parts:
        return text, '', '', ''

    # Extract gender from last word
    gender = ''
    last = parts[-1]
    if last in _TELUGU_GENDER_MAP:
        gender = _TELUGU_GENDER_MAP[last]
        parts = parts[:-1]
    # Handle "సస సస" (two-word female)
    if len(parts) >= 1 and parts[-1] in _TELUGU_GENDER_MAP and not gender:
        gender = _TELUGU_GENDER_MAP[parts[-1]]
        parts = parts[:-1]

    # Find relation code
    rel_idx = -1
    for i, p in enumerate(parts):
        if p in _TELUGU_REL_CODES:
            rel_idx = i
            break

    if rel_idx >= 0:
        elector_name = ' '.join(parts[:rel_idx])
        relation = parts[rel_idx]
        relation_name = ' '.join(parts[rel_idx + 1:])
    else:
        elector_name = ' '.join(parts)
        relation = ''
        relation_name = ''

    return elector_name, relation, relation_name, gender


def _relation_to_standard(rel_code):
    """Map Telugu relation code to standard English relation type."""
    if not rel_code:
        return ''
    # Husband variants
    if rel_code in ('భ', 'ఇ'):
        return 'Husband'
    # Mother
    if rel_code == 'తల':
        return 'Mother'
    # Other
    if rel_code == 'మత':
        return 'Other'
    # All other త-prefixed codes are father variants
    if rel_code.startswith('త'):
        return 'Father'
    return ''


# Standard output columns (superset: Telugu-specific + standard)
_POSTPROCESS_FIELDS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "ps_no", "serial_no", "section_no", "house_no",
    "elector_name_telugu", "relation_telugu", "relation_name_telugu",
    "gender_telugu",
    "elector_name", "relation_type", "relation_name", "sex",
    "age", "epic_no", "roll_year",
]


# ── Transliteration ──────────────────────────────────────────────────────

def _transliterate_telugu(text):
    """Transliterate Telugu text to English (Latin) using IAST + diacritic stripping."""
    if not text or not text.strip():
        return ''
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate
    iast = transliterate(text.strip(), sanscript.TELUGU, sanscript.IAST)
    diacritic_map = {
        'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
        'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
        'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm',
    }
    for k, v in diacritic_map.items():
        iast = iast.replace(k, v)
    # Telugu: do NOT apply schwa deletion — final vowels are real
    return ' '.join(iast.split()).title()


def _is_already_english(text):
    """Return True if text contains no Telugu characters (already English)."""
    if not text or not text.strip():
        return True
    return not bool(TELUGU_RE.search(text))


# Stray Telugu gender codes that leak into English-AC relation names
_STRAY_TELUGU_RE = re.compile(r'\s*[' + ''.join(set('పవసస')) + r']+\s*$')


def _clean_english_name(text):
    """Clean an English-format name: strip stray Telugu gender codes, title-case."""
    if not text or not text.strip():
        return ''
    # Remove trailing Telugu gender codes (పప, సస, etc.)
    cleaned = _STRAY_TELUGU_RE.sub('', text).strip()
    if not cleaned:
        return text.strip().title()
    return cleaned.title()


def _clean_for_transliteration(text):
    """For mostly-English text with some Telugu remnants, strip Telugu chars."""
    if not text or not text.strip():
        return ''
    # If mostly English (more Latin than Telugu chars), strip Telugu and title-case
    latin_count = sum(1 for c in text if c.isascii() and c.isalpha())
    telugu_count = sum(1 for c in text if '\u0C00' <= c <= '\u0C7F')
    if latin_count > telugu_count:
        # Strip Telugu characters
        cleaned = re.sub(r'[\u0C00-\u0C7F]+', '', text).strip()
        return ' '.join(cleaned.split()).title() if cleaned else text.strip().title()
    return None  # Proceed with full transliteration


_TRANSLITERATE_FIELDS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "ps_no", "serial_no", "section_no", "house_no",
    "elector_name_telugu", "relation_telugu", "relation_name_telugu",
    "gender_telugu",
    "elector_name", "relation_type", "relation_name", "sex",
    "age", "epic_no", "roll_year",
    "elector_name_en", "relation_name_en", "relation_type_en", "sex_en",
]

# Telugu relation type mapping for transliteration
_REL_TYPE_TELUGU_MAP = {
    'తండ్రి': 'Father', 'తత': 'Father',
    'భర్త': 'Husband', 'భ': 'Husband',
    'తల్లి': 'Mother', 'తల': 'Mother',
    'మత': 'Other',
}


def _transliterate_csv(csv_path):
    """Add English transliteration columns to a postprocessed Telangana CSV.

    Adds: elector_name_en, relation_name_en, relation_type_en, sex_en
    For already-English names (Hyderabad ACs 206-218), just title-cases them.
    For Telugu names, transliterates via IAST.
    """
    import tempfile, shutil

    stats = {"total": 0, "transliterated": 0, "english_passthrough": 0}

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=_TRANSLITERATE_FIELDS)
            writer.writeheader()
            for row in reader:
                stats["total"] += 1

                ename = row.get("elector_name_telugu", "").strip()
                rname = row.get("relation_name_telugu", "").strip()
                rel_code = row.get("relation_telugu", "").strip()

                # Transliterate elector name
                if _is_already_english(ename):
                    row["elector_name_en"] = _clean_english_name(ename)
                    stats["english_passthrough"] += 1
                else:
                    # Check if mostly English with stray Telugu
                    cleaned = _clean_for_transliteration(ename)
                    if cleaned is not None:
                        row["elector_name_en"] = cleaned
                        stats["english_passthrough"] += 1
                    else:
                        row["elector_name_en"] = _transliterate_telugu(ename)
                        stats["transliterated"] += 1

                # Transliterate relation name — strip trailing Telugu gender codes first
                rname_clean = rname
                for gcode in ('సస', 'పప', 'పవ', 'ప', 'స', 'వ'):
                    if rname_clean.endswith(' ' + gcode):
                        rname_clean = rname_clean[:-(len(gcode) + 1)].strip()
                        break

                if _is_already_english(rname_clean):
                    row["relation_name_en"] = _clean_english_name(rname_clean)
                else:
                    cleaned = _clean_for_transliteration(rname_clean)
                    if cleaned is not None:
                        row["relation_name_en"] = cleaned
                    else:
                        row["relation_name_en"] = _transliterate_telugu(rname_clean)

                # relation_type_en: copy from relation_type (already mapped)
                rt = row.get("relation_type", "").strip()
                if rt in ("Father", "Husband", "Mother", "Other"):
                    row["relation_type_en"] = rt
                else:
                    # Try mapping from Telugu code
                    row["relation_type_en"] = _REL_TYPE_TELUGU_MAP.get(rel_code, rt)

                # sex_en: copy from sex (already mapped)
                sex = row.get("sex", "").strip()
                row["sex_en"] = sex if sex in ("M", "F") else ""

                out = {k: row.get(k, "") for k in _TRANSLITERATE_FIELDS}
                writer.writerow(out)

                if stats["total"] % 2_000_000 == 0:
                    print(f"  ... {stats['total']:,} rows processed", flush=True)

        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Transliterate {csv_path}:")
    print(f"  {stats['total']:,} rows total")
    print(f"  {stats['transliterated']:,} rows transliterated (Telugu → English)")
    print(f"  {stats['english_passthrough']:,} rows English passthrough")


def _postprocess(csv_path):
    """Fix column-bleeding issues and add standard columns to an extracted CSV.

    Telangana-specific fixes:
    1. The extractor dumps all Telugu text into elector_name_telugu with
       relation_telugu, relation_name_telugu, gender_telugu left empty.
       Split the concatenated text into proper columns using Telugu keywords:
       - Relation: తత (father), భ (husband), తల (mother), మత (other)
       - Gender: పప/పవ (male), సస (female) — always last word
    2. Add standard columns (elector_name, sex, relation_type, relation_name)
       by copying/mapping from the Telugu-specific columns.
    3. Ensure roll_year is present.
    """
    import tempfile, shutil

    fixes = {"split": 0, "total": 0, "std_filled": 0}

    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path),
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=_POSTPROCESS_FIELDS)
            writer.writeheader()
            for row in reader:
                fixes["total"] += 1
                telugu = row.get("elector_name_telugu", "").strip()

                if telugu and (not row.get("gender_telugu", "").strip() or not row.get("relation_telugu", "").strip()):
                    # Re-combine if partially split (relation empty but gender set)
                    full_text = telugu
                    if row.get("relation_name_telugu", "").strip():
                        full_text = telugu + " " + row.get("relation_telugu", "") + " " + row.get("relation_name_telugu", "")
                    if row.get("gender_telugu", "").strip() == "M":
                        full_text += " పప"
                    elif row.get("gender_telugu", "").strip() == "F":
                        full_text += " సస"
                    name, rel, rel_name, gender = _split_telugu_text(full_text.strip())
                    row["elector_name_telugu"] = name
                    row["relation_telugu"] = rel
                    row["relation_name_telugu"] = rel_name
                    row["gender_telugu"] = gender
                    fixes["split"] += 1

                # Standard columns: copy Telugu name as elector_name,
                # map gender_telugu (M/F) to sex, relation code to type
                row["elector_name"] = row.get("elector_name_telugu", "")
                gender_val = row.get("gender_telugu", "").strip()
                row["sex"] = gender_val if gender_val in ("M", "F") else ""
                row["relation_type"] = _relation_to_standard(row.get("relation_telugu", "").strip())
                row["relation_name"] = row.get("relation_name_telugu", "")

                # Ensure roll_year
                if not row.get("roll_year"):
                    row["roll_year"] = str(ROLL_YEAR)

                fixes["std_filled"] += 1

                # Write only the fields we want (handles missing/extra cols)
                out = {k: row.get(k, "") for k in _POSTPROCESS_FIELDS}
                writer.writerow(out)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows processed")
    print(f"  {fixes['split']} rows split into name/relation/gender")
    print(f"  {fixes['std_filled']:,} rows with standard columns filled")


def main():
    ap = argparse.ArgumentParser(description="Extract Telangana SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None, help="process first N ACs")
    ap.add_argument("--out-dir", default="output/csv/telangana")
    ap.add_argument("--combined", action="store_true", help="single CSV for state")
    ap.add_argument("--postprocess", metavar="CSV", default=None,
                    help="post-process an existing CSV to fix column bleeding")
    ap.add_argument("--transliterate", metavar="CSV", default=None,
                    help="add English transliteration columns to a postprocessed CSV")
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

        # Build corrected Gautami CMap for this AC (if decoder available)
        corrected_cmap = None
        if _HAS_FONT_DECODER and 206 <= ac_no <= 218:
            pass  # English-only Hyderabad ACs — no Gautami font
        elif _HAS_FONT_DECODER:
            try:
                corrected_cmap = build_corrected_cmap(zip_path)
            except Exception as exc:
                print(f"(font decoder failed: {exc})", end=" ", flush=True)

        with zipfile.ZipFile(zip_path) as z:
            part_files = sorted([n for n in z.namelist() if n.endswith(".pdf")])
            ac_rows = []
            for pf in part_files:
                m = re.search(r"(\d+)", os.path.basename(pf))
                if not m:
                    continue
                part_no = int(m.group(1))
                records = extract_pdf(z.read(pf), corrected_cmap=corrected_cmap)
                for rec in records:
                    ac_rows.append([
                        STATE_ID, district, ac_no, ac_name, part_no,
                        rec["ps_no"], rec["sl_no"], rec["section_no"],
                        rec["house_no"], rec["telugu"], "", "", "",
                        rec["age"], rec["epic_no"],
                        ROLL_YEAR,
                    ])

        if args.combined:
            all_rows.extend(ac_rows)
        else:
            csv_path = os.path.join(args.out_dir, f"AC{ac_no:03d}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADERS)
                w.writerows(ac_rows)

        print(f"{len(ac_rows)} rows")

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
