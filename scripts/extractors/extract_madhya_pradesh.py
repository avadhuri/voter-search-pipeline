"""
Extract Madhya Pradesh old SIR (2003) voter roll PDFs to CSV.

PDF has ruled tables with 8 columns extractable via pdfplumber.extract_tables():
  Serial, House, Elector_Name, Relation(Father/Husband), Relation_Name, Gender, Age, EPIC

Text is in CDAC_GISTSurekh font — a CDAC/GIST-family Devanagari font that uses
Private Use Area (PUA) codepoints. The PDF's ToUnicode CMap maps these to WRONG
standard Unicode. Each PDF has a different font subset with different byte-to-glyph
assignments, so a fixed mapping table cannot work across ACs.

Decoder strategy:
  1. Monkey-patch pdfminer to output PUA codepoints for GISTSurekh fonts.
  2. For each PDF, extract the embedded font file and fingerprint each glyph
     using its contour data (MD5 hash of coordinates+flags+endpoints).
  3. Match fingerprints against a master table (built from AC001 visual analysis)
     to determine the correct Unicode for each glyph.
  4. Build a per-PDF byte -> correct_unicode decode table.
  5. Apply the decode table to extracted text, with pre-base ि reordering.

Usage:
    python scripts/extractors/extract_madhya_pradesh.py --ac 1 --out-dir /tmp/mp_test
    python scripts/extractors/extract_madhya_pradesh.py --combined
    python scripts/extractors/extract_madhya_pradesh.py --postprocess output/csv/madhya_pradesh/AC001.csv
"""
import argparse, csv, hashlib, io, json, os, re, shutil, tempfile, unicodedata, zipfile
import pdfplumber
from fontTools.ttLib import TTFont
from pdfminer.pdffont import PDFSimpleFont
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1
from pdfminer.psparser import PSLiteral

STATE_ID = "madhya_pradesh"
ROLL_YEAR = 2003
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "sex", "age", "epic_no",
    "roll_year",
]


# ── CDAC_GISTSurekh → Unicode Devanagari decoder ──────────────────────────

def _glyph_fingerprint(glyf_table, glyph_name):
    """Create a stable fingerprint from glyph outline data."""
    g = glyf_table[glyph_name]
    if g.numberOfContours == 0:
        return "empty"
    if g.numberOfContours == -1:
        return "composite"
    coords = tuple((x, y) for x, y in g.coordinates)
    flags = tuple(g.flags)
    ends = tuple(g.endPtsOfContours)
    return hashlib.md5(str((coords, flags, ends)).encode()).hexdigest()


# Master fingerprint → correct Unicode table.
# Built from AC001 visual analysis (verified by rendering PDF pages and
# matching byte sequences from content streams against rendered text).
# Key = MD5 fingerprint of glyph contour data.
# Value = correct Unicode string.
#
# This table is populated at module load time by _build_master_table().
_MASTER_FP_TABLE = {}

# AC001 PUA byte → correct Unicode (verified mapping).
_GIST_AC001 = {
    0x20: ' ', 0x21: '1', 0x22: 'स', 0x23: 'ु', 0x24: 'ल्', 0x25: 'त',
    0x26: 'ा', 0x27: 'न', 0x28: 'ब', 0x29: 'ू', 0x2A: 'ल', 0x2B: '2',
    0x2C: 'ि', 0x2D: 'प', 0x2E: 'रू', 0x2F: 'ष', 0x30: 'क', 0x31: 'व',
    0x32: 'म', 0x33: '्', 0x34: 'ई', 0x35: '8', 0x36: 'ह', 0x37: '3',
    0x38: 'च', 0x39: 'र', 0x3A: 'ज', 0x3B: 'य', 0x3C: '7', 0x3D: 'M',
    0x3E: 'P', 0x3F: '/', 0x40: '0', 0x41: '4', 0x42: 'े', 0x43: 'द',
    0x44: 'ी', 0x45: '6', 0x46: '9', 0x47: '5', 0x48: 'ख', 0x49: 'द्र',
    0x4A: 'ो', 0x4B: 'ं', 0x4C: 'फ', 0x4D: 'फ़', 0x4E: 'ा', 0x4F: '॰',
    0x50: '.', 0x51: 'ग', 0x52: 'फ्', 0x53: 'ड', 0x54: 'ट्', 0x55: 'घ',
    0x56: 'श', 0x57: 'ण', 0x58: 'अ', 0x59: 'भ', 0x5A: 'ग़', 0x5B: 'ध',
    0x5C: 'थ', 0x5D: 'त्', 0x5E: 'द्', 0x5F: 'a', 0x60: 'g', 0x61: 'e',
    0x62: 'o', 0x63: 'f', 0x64: 'उ', 0x65: 'न्न', 0x66: 'न्', 0x67: 'श्र',
    0x68: 'प्र', 0x69: 'क्ष्', 0x6A: 'ज्', 0x6B: 'िं', 0x6C: 'ड्',
    0x6D: 'श्', 0x6E: 'छ', 0x6F: 'स्', 0x70: 'त्त', 0x71: 'ग्', 0x72: 'व्',
    0x73: 'ओ', 0x74: 'ँ', 0x75: 'औ', 0x76: 'ड़', 0x77: 'द्ध', 0x78: 'क्र',
    0x79: 'ब्', 0x7A: 'ँ', 0x7B: 'ट', 0x7C: 'ल्', 0x7D: 'क्', 0x7E: 'र्',
    0x7F: 'इ', 0x80: 'ा', 0x81: 'ॅ', 0x82: 'ढ', 0x83: '़', 0x84: 'ष्',
    0x85: 'आ', 0x86: 'ज़', 0x87: 'ठ्', 0x88: 'ठ', 0x89: 'ब्र', 0x8A: 'य्',
    0x8B: 'ए', 0x8C: 'ऐ', 0x8D: 'ा', 0x8E: 'ऊ', 0x8F: 'ध्', 0x90: 'ा',
    0x91: '्र', 0x92: 'झ', 0x93: 'ै', 0x94: 'ौ',
}


def _build_master_table():
    """Build master fingerprint table from AC001's embedded font."""
    global _MASTER_FP_TABLE
    if _MASTER_FP_TABLE:
        return  # already built

    ac001_path = os.path.join("data", "raw", STATE_ID, "AC001.zip")
    if not os.path.exists(ac001_path):
        return

    z = zipfile.ZipFile(ac001_path)
    pdfs = sorted([n for n in z.namelist() if n.endswith('.pdf')])
    if not pdfs:
        return

    pdf_bytes = z.read(pdfs[0])
    parser = PDFParser(io.BytesIO(pdf_bytes))
    doc = PDFDocument(parser)

    for page in PDFPage.create_pages(doc):
        resources = resolve1(page.resources)
        fonts = resolve1(resources.get('Font', {}))
        for fname, fref in fonts.items():
            font_obj = resolve1(fref)
            base_font = str(font_obj.get('BaseFont'))
            if 'GISTSurekh' not in base_font:
                continue
            desc = resolve1(font_obj.get('FontDescriptor'))
            if 'FontFile2' not in desc:
                continue
            ff_data = resolve1(desc['FontFile2']).get_data()
            ttfont = TTFont(io.BytesIO(ff_data))
            glyf = ttfont['glyf']

            for gname in ttfont.getGlyphOrder():
                if gname == '.notdef':
                    continue
                fp = _glyph_fingerprint(glyf, gname)
                byte_code = int(gname.replace('uniF0', ''), 16)
                if byte_code in _GIST_AC001:
                    _MASTER_FP_TABLE[fp] = _GIST_AC001[byte_code]
        break


def _build_pdf_decode_table(pdf_bytes):
    """Build a byte → correct_unicode decode table for a specific PDF.

    Extracts the embedded GISTSurekh font, fingerprints each glyph,
    and matches against the master fingerprint table.
    Returns a dict mapping PUA byte codes to correct Unicode strings.
    """
    _build_master_table()

    decode = {}
    parser = PDFParser(io.BytesIO(pdf_bytes))
    doc = PDFDocument(parser)

    for page in PDFPage.create_pages(doc):
        resources = resolve1(page.resources)
        fonts = resolve1(resources.get('Font', {}))
        for fname, fref in fonts.items():
            font_obj = resolve1(fref)
            base_font = str(font_obj.get('BaseFont'))
            if 'GISTSurekh' not in base_font:
                continue
            desc = resolve1(font_obj.get('FontDescriptor'))
            if 'FontFile2' not in desc:
                continue
            ff_data = resolve1(desc['FontFile2']).get_data()
            ttfont = TTFont(io.BytesIO(ff_data))
            glyf = ttfont['glyf']

            for gname in ttfont.getGlyphOrder():
                if gname == '.notdef':
                    continue
                fp = _glyph_fingerprint(glyf, gname)
                byte_code = int(gname.replace('uniF0', ''), 16)
                if fp in _MASTER_FP_TABLE:
                    decode[byte_code] = _MASTER_FP_TABLE[fp]
                else:
                    decode[byte_code] = '�'
        break  # first page has all font definitions

    return decode


def _gist_decode(s, decode_table):
    """Decode a GIST PUA string using a per-PDF decode table."""
    out = []
    for ch in s:
        code = ord(ch)
        if 0xF020 <= code <= 0xF0FF:
            byte_code = code - 0xF000
            out.append(decode_table.get(byte_code, '�'))
        else:
            out.append(ch)
    t = ''.join(out)

    # Reorder pre-base ि and िं
    t = re.sub(r'िं([क-ह])', r'\1िं', t)
    t = re.sub(r'ि([क-ह])', r'\1ि', t)

    # Clean up ligature sequences
    t = t.replace('ाे', 'ो')
    t = t.replace('ाै', 'ौ')

    return unicodedata.normalize('NFC', t)


# ── Monkey-patch pdfminer ──────────────────────────────────────────────────

_orig_to_unichr = PDFSimpleFont.to_unichr

def _patched_to_unichr(self, cid):
    basefont = getattr(self, 'basefont', b'')
    if isinstance(basefont, bytes):
        basefont = basefont.decode('ascii', errors='ignore')
    elif isinstance(basefont, PSLiteral):
        basefont = basefont.name
        if isinstance(basefont, bytes):
            basefont = basefont.decode('ascii', errors='ignore')
    if 'GISTSurekh' in str(basefont):
        return chr(0xF000 + cid)
    return _orig_to_unichr(self, cid)

def _install_gist_patch():
    PDFSimpleFont.to_unichr = _patched_to_unichr

def _uninstall_gist_patch():
    PDFSimpleFont.to_unichr = _orig_to_unichr


# ── PDF extraction ─────────────────────────────────────────────────────────

def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def _is_pua(s):
    return any(0xF020 <= ord(ch) <= 0xF0FF for ch in s)


def extract_pdf_table(pdf_bytes):
    """Extract table rows from a single PDF, decoding GIST text."""
    # Build per-PDF decode table from embedded font fingerprints
    decode_table = _build_pdf_decode_table(pdf_bytes)

    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 7:
                        continue
                    sl = (row[0] or "").strip()
                    if _is_pua(sl):
                        sl = _gist_decode(sl, decode_table).strip()
                    if sl and sl.isdigit():
                        cells = []
                        for c in row[:8]:
                            cell = str(c or "").strip().replace("\n", " ")
                            if _is_pua(cell):
                                cell = _gist_decode(cell, decode_table)
                            cells.append(cell)
                        rows.append(cells)
    return rows


# ── Post-processing ────────────────────────────────────────────────────────

VALID_SEX = {"पुरूष", "पुरुष", "महिला", "अन्य", ""}
VALID_RELATION = {"पिता", "पति", "माता", "अन्य", ""}
SEX_NORMALIZE = {"पुरूष": "पुरुष"}


def _postprocess(csv_path):
    fixes = {"total": 0, "sex_fixed": 0, "rel_fixed": 0, "age_fixed": 0}
    tmp = tempfile.NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                      dir=os.path.dirname(csv_path) or ".",
                                      suffix=".csv", delete=False)
    try:
        with open(csv_path, encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            writer = csv.DictWriter(tmp, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                fixes["total"] += 1
                sex = row.get("sex", "").strip()
                if sex in SEX_NORMALIZE:
                    row["sex"] = SEX_NORMALIZE[sex]
                    fixes["sex_fixed"] += 1
                elif sex and sex not in VALID_SEX:
                    row["sex"] = ""
                    fixes["sex_fixed"] += 1
                rel = row.get("relation", "").strip()
                if rel and rel not in VALID_RELATION:
                    row["relation"] = ""
                    fixes["rel_fixed"] += 1
                age = row.get("age", "").strip()
                if age:
                    try:
                        a = int(age)
                        if a < 0 or a > 120:
                            row["age"] = ""
                            fixes["age_fixed"] += 1
                    except ValueError:
                        row["age"] = ""
                        fixes["age_fixed"] += 1
                writer.writerow(row)
        tmp.close()
        shutil.move(tmp.name, csv_path)
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise
    print(f"Postprocess {csv_path}:")
    print(f"  {fixes['total']:,} rows, {fixes['sex_fixed']} sex, "
          f"{fixes['rel_fixed']} rel, {fixes['age_fixed']} age fixed")


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


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Extract Madhya Pradesh SIR PDFs to CSV")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out-dir", default="output/csv/madhya_pradesh")
    ap.add_argument("--combined", action="store_true")
    ap.add_argument("--postprocess", metavar="CSV", default=None)
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
    _install_gist_patch()

    try:
        all_rows = []
        for zf in zip_files:
            ac_no = int(re.search(r"\d+", zf).group())
            ac_info = ac_map.get(ac_no, {})
            ac_name = ac_info.get("ac_name", "")
            district = ac_info.get("district_name", "")
            print(f"  {zf}: AC{ac_no:03d} {ac_name}...", end=" ", flush=True)

            with zipfile.ZipFile(os.path.join(raw_dir, zf)) as z:
                ac_rows = []
                for pf in sorted(n for n in z.namelist() if n.endswith(".pdf")):
                    m = re.search(r"(\d+)", os.path.basename(pf))
                    if not m:
                        continue
                    part_no = int(m.group(1))
                    for row in extract_pdf_table(z.read(pf)):
                        r = (row + [""] * 8)[:8]
                        ac_rows.append([
                            STATE_ID, district, ac_no, ac_name, part_no,
                            r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
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
            path = os.path.join(args.out_dir, f"{STATE_ID}_combined.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADERS)
                w.writerows(all_rows)
            print(f"Combined: {path} ({len(all_rows)} rows)")
    finally:
        _uninstall_gist_patch()


if __name__ == "__main__":
    main()
