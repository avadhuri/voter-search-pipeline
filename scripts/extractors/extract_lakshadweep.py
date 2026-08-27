"""
Extract Lakshadweep old SIR (2002) voter roll PDFs to CSV.

PDF has ruled tables with 14 columns extractable via pdfplumber.extract_tables():
  District, AC_No, Island, Part, Serial, House, Elector_Name,
  Relation, Relation_Name, Gender_Local, Gender, Age, EPIC, Status

Names are in Malayalam.  The PDFs use anonymous CID fonts (CIDFont+F1/F2/F3)
whose ToUnicode CMaps only cover basic Malayalam characters.  ~108 glyphs for
conjuncts, half-forms, and post-base signs are missing from the CMap and
pdfplumber emits them as ``(cid:NNN)`` placeholders.  The CID_MAP below was
reverse-engineered from the embedded TrueType font tables (cmap + GSUB
ligature/single-substitution lookups) and maps every observed CID back to its
correct Malayalam Unicode sequence.

Usage:
    python scripts/extractors/extract_lakshadweep.py
    python scripts/extractors/extract_lakshadweep.py --combined
    python scripts/extractors/extract_lakshadweep.py --postprocess
"""
import argparse, csv, io, json, os, re, unicodedata, zipfile
import pdfplumber

STATE_ID = "lakshadweep"
ROLL_YEAR = 2002
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "gender_local", "sex", "age", "epic_no", "status",
    "roll_year",
]

# ---------------------------------------------------------------------------
# CID font decoder: maps (cid:NNN) placeholders to Malayalam Unicode.
#
# Built by extracting the embedded CIDFont+F2 and CIDFont+F3 TrueType fonts
# from the Lakshadweep PDFs and tracing their GSUB substitution lookups
# (ligature, single-sub, extension) back to the input Unicode sequences.
#
# CIDFont+F2 (main Malayalam text font):
#   - CIDs 299-336: half-forms (consonant + virama)
#   - CIDs 342-345: post-base signs (ya, ra, va)
#   - CIDs 346-397: consonant conjuncts (two consonants joined by virama)
#   - CIDs 398-418: consonant + la conjuncts
#   - CID 419: post-base ya (alternate form)
#   - CIDs 423-424: vowel sign alternates (u, uu)
#
# CIDFont+F3 (used in some parts, different GID layout):
#   - CIDs 1519-1576: conjuncts (same Malayalam sequences, different GIDs)
#   - CID 1618: pre-base reph (virama + ra)
# ---------------------------------------------------------------------------
CID_MAP = {
    # --- CIDFont+F2: Half-forms (consonant + virama) ---
    299: "\u0D32\u0D4D",      # ല്
    301: "\u0D15\u0D4D",      # ക്
    302: "\u0D16\u0D4D",      # ഖ്
    303: "\u0D17\u0D4D",      # ഗ്
    304: "\u0D18\u0D4D",      # ഘ്
    305: "\u0D19\u0D4D",      # ങ്
    306: "\u0D1A\u0D4D",      # ച്
    308: "\u0D1C\u0D4D",      # ജ്
    310: "\u0D1E\u0D4D",      # ഞ്
    311: "\u0D1F\u0D4D",      # ട്
    312: "\u0D20\u0D4D",      # ഠ്
    313: "\u0D21\u0D4D",      # ഡ്
    315: "\u0D23\u0D4D",      # ണ്
    316: "\u0D24\u0D4D",      # ത്
    317: "\u0D25\u0D4D",      # ഥ്
    318: "\u0D26\u0D4D",      # ദ്
    319: "\u0D27\u0D4D",      # ധ്
    320: "\u0D28\u0D4D",      # ന്
    321: "\u0D2A\u0D4D",      # പ്
    322: "\u0D2B\u0D4D",      # ഫ്
    323: "\u0D2C\u0D4D",      # ബ്
    324: "\u0D2D\u0D4D",      # ഭ്
    325: "\u0D2E\u0D4D",      # മ്
    326: "\u0D2F\u0D4D",      # യ്
    327: "\u0D30\u0D4D",      # ര്
    328: "\u0D31\u0D4D",      # റ്
    329: "\u0D32\u0D4D",      # ല്  (alternate half-la via different lookup)
    330: "\u0D33\u0D4D",      # ള്
    331: "\u0D34\u0D4D",      # ഴ്
    332: "\u0D35\u0D4D",      # വ്
    333: "\u0D36\u0D4D",      # ശ്
    334: "\u0D37\u0D4D",      # ഷ്
    335: "\u0D38\u0D4D",      # സ്
    336: "\u0D39\u0D4D",      # ഹ്

    # --- CIDFont+F2: Post-base signs ---
    342: "\u0D4D\u0D2F",      # ്യ  (post-base ya)
    343: "\u0D4D\u0D30",      # ്ര  (post-base ra)
    345: "\u0D4D\u0D35",      # ്വ  (post-base va)

    # --- CIDFont+F2: Consonant conjuncts ---
    346: "\u0D15\u0D4D\u0D15",  # ക്ക
    347: "\u0D15\u0D4D\u0D24",  # ക്ത
    348: "\u0D15\u0D4D\u0D37",  # ക്ഷ
    349: "\u0D17\u0D4D\u0D17",  # ഗ്ഗ
    350: "\u0D17\u0D4D\u0D28",  # ഗ്ന
    351: "\u0D17\u0D4D\u0D2E",  # ഗ്മ
    352: "\u0D19\u0D4D\u0D15",  # ങ്ക
    353: "\u0D19\u0D4D\u0D19",  # ങ്ങ
    354: "\u0D1A\u0D4D\u0D1A",  # ച്ച
    355: "\u0D1A\u0D4D\u0D1B",  # ച്ഛ
    356: "\u0D1C\u0D4D\u0D1C",  # ജ്ജ
    357: "\u0D1C\u0D4D\u0D1E",  # ജ്ഞ
    358: "\u0D1E\u0D4D\u0D1A",  # ഞ്ച
    359: "\u0D1E\u0D4D\u0D1E",  # ഞ്ഞ
    360: "\u0D1F\u0D4D\u0D1F",  # ട്ട
    361: "\u0D21\u0D4D\u0D21",  # ഡ്ഡ
    362: "\u0D23\u0D4D\u0D1F",  # ണ്ട
    363: "\u0D23\u0D4D\u0D21",  # ണ്ഡ
    364: "\u0D23\u0D4D\u0D23",  # ണ്ണ
    365: "\u0D23\u0D4D\u0D2E",  # ണ്മ
    366: "\u0D24\u0D4D\u0D24",  # ത്ത
    369: "\u0D24\u0D4D\u0D2E",  # ത്മ
    370: "\u0D24\u0D4D\u0D38",  # ത്സ
    371: "\u0D26\u0D4D\u0D26",  # ദ്ദ
    372: "\u0D26\u0D4D\u0D27",  # ദ്ധ
    373: "\u0D28\u0D4D\u0D24",  # ന്ത
    375: "\u0D28\u0D4D\u0D26",  # ന്ദ
    376: "\u0D28\u0D4D\u0D27",  # ന്ധ
    377: "\u0D28\u0D4D\u0D28",  # ന്ന
    378: "\u0D28\u0D4D\u0D2E",  # ന്മ
    379: "\u0D28\u0D4D\u0D31",  # ന്റ
    380: "\u0D2A\u0D4D\u0D2A",  # പ്പ
    381: "\u0D2C\u0D4D\u0D26",  # ബ്ദ
    382: "\u0D2C\u0D4D\u0D27",  # ബ്ധ
    383: "\u0D2C\u0D4D\u0D2C",  # ബ്ബ
    384: "\u0D2E\u0D4D\u0D2A",  # മ്പ
    385: "\u0D2E\u0D4D\u0D2E",  # മ്മ
    386: "\u0D2F\u0D4D\u0D2F",  # യ്യ
    387: "\u0D31\u0D4D\u0D31",  # റ്റ
    388: "\u0D33\u0D4D\u0D33",  # ള്ള
    389: "\u0D35\u0D4D\u0D35",  # വ്വ
    391: "\u0D36\u0D4D\u0D36",  # ശ്ശ
    393: "\u0D38\u0D4D\u0D25",  # സ്ഥ
    394: "\u0D38\u0D4D\u0D31\u0D4D\u0D31",  # സ്റ്റ
    395: "\u0D38\u0D4D\u0D38",  # സ്സ
    396: "\u0D39\u0D4D\u0D28",  # ഹ്ന
    397: "\u0D39\u0D4D\u0D2E",  # ഹ്മ

    # --- CIDFont+F2: Consonant + la conjuncts ---
    398: "\u0D15\u0D4D\u0D32",  # ക്ല
    400: "\u0D17\u0D4D\u0D32",  # ഗ്ല
    406: "\u0D26\u0D4D\u0D32",  # ദ്ല
    407: "\u0D2A\u0D4D\u0D32",  # പ്ല
    409: "\u0D2C\u0D4D\u0D32",  # ബ്ല
    417: "\u0D38\u0D4D\u0D32",  # സ്ല
    418: "\u0D39\u0D4D\u0D32",  # ഹ്ല

    # --- CIDFont+F2: Post-base ya (alternate) ---
    419: "\u0D4D\u0D2F",      # ്യ

    # --- CIDFont+F2: Vowel sign alternates ---
    423: "\u0D41",             # ു
    424: "\u0D42",             # ൂ

    # --- CIDFont+F3: Conjuncts (same Malayalam, different font GIDs) ---
    1519: "\u0D15\u0D4D\u0D15",  # ക്ക
    1528: "\u0D19\u0D4D\u0D15",  # ങ്ക
    1530: "\u0D1A\u0D4D\u0D1A",  # ച്ച
    1536: "\u0D1E\u0D4D\u0D1A",  # ഞ്ച
    1537: "\u0D1E\u0D4D\u0D1E",  # ഞ്ഞ
    1538: "\u0D1F\u0D4D\u0D1F",  # ട്ട
    1547: "\u0D24\u0D4D\u0D24",  # ത്ത
    1559: "\u0D28\u0D4D\u0D28",  # ന്ന
    1562: "\u0D2A\u0D4D\u0D2A",  # പ്പ
    1570: "\u0D2E\u0D4D\u0D2A",  # മ്പ
    1571: "\u0D2E\u0D4D\u0D2E",  # മ്മ
    1574: "\u0D31\u0D4D\u0D31",  # റ്റ
    1576: "\u0D32\u0D4D\u0D32",  # ല്ല

    # --- CIDFont+F3: Pre-base reph ---
    1618: "\u0D4D\u0D30",      # ്ര
}

_CID_RE = re.compile(r"\(cid:(\d+)\)")


def decode_cid(text):
    """Replace all (cid:NNN) placeholders with their Malayalam Unicode."""
    def _repl(m):
        cid = int(m.group(1))
        return CID_MAP.get(cid, m.group(0))   # keep original if unknown
    return _CID_RE.sub(_repl, text)


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def extract_pdf_table(pdf_bytes):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 12:
                        continue
                    sl = (row[4] or "").strip()
                    if sl and sl.isdigit():
                        rows.append([decode_cid(str(c or "").strip()) for c in row[:14]])
    return rows


def postprocess_csv(in_path, out_path):
    """Read an existing CSV, decode CID placeholders, and fix column bleeding.

    Fixes:
    - Newlines in elector_name / relation_name (long names wrapped in PDF) → join with space
    - Newlines in age (stacked cell values) → take first number
    - Malayalam text bleeding into sex column → extract M/F
    - Re-derive sex_en / relation_type_en after fixes
    """
    import tempfile, shutil

    _sex_extract_re = re.compile(r'[MF]')

    with open(in_path, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames)

        tmp = tempfile.NamedTemporaryFile(
            mode='w', newline='', encoding='utf-8',
            dir=os.path.dirname(in_path), suffix='.csv', delete=False)
        try:
            writer = csv.DictWriter(tmp, fieldnames=fields)
            writer.writeheader()
            stats = {"newline_name": 0, "newline_rel": 0, "newline_age": 0,
                     "bad_sex": 0, "cid_resolved": 0, "total": 0}

            for row in reader:
                stats["total"] += 1

                # --- CID decode all fields ---
                for k in row:
                    if row[k] and _CID_RE.search(row[k]):
                        before_cids = len(_CID_RE.findall(row[k]))
                        row[k] = decode_cid(row[k])
                        after_cids = len(_CID_RE.findall(row[k]))
                        stats["cid_resolved"] += before_cids - after_cids

                # --- Fix newlines in name fields ---
                for col in ("elector_name", "relation_name"):
                    val = row.get(col, "")
                    if val and "\n" in val:
                        row[col] = " ".join(val.split())
                        stats["newline_name" if col == "elector_name" else "newline_rel"] += 1

                # --- Fix newlines in age (take first number) ---
                age = row.get("age", "")
                if age and "\n" in age:
                    first = age.split("\n")[0].strip()
                    row["age"] = first if first.isdigit() else ""
                    stats["newline_age"] += 1

                # --- Fix bad sex values ---
                sex = row.get("sex", "").strip()
                if sex and sex not in ("M", "F"):
                    m = _sex_extract_re.search(sex)
                    row["sex"] = m.group(0) if m else ""
                    stats["bad_sex"] += 1

                # --- Re-derive _en columns if present ---
                if "sex_en" in fields:
                    row["sex_en"] = _SEX_EN.get(row.get("sex", "").strip(), "")
                if "relation_type_en" in fields:
                    row["relation_type_en"] = _REL_EN.get(
                        row.get("relation", "").strip(), "")

                writer.writerow(row)

            tmp.close()
            shutil.move(tmp.name, out_path)

            print(f"Postprocessed {in_path} -> {out_path}")
            print(f"  {stats['total']:,} rows")
            print(f"  CID placeholders resolved: {stats['cid_resolved']}")
            print(f"  Newlines fixed: {stats['newline_name']} names, "
                  f"{stats['newline_rel']} relations, {stats['newline_age']} ages")
            print(f"  Bad sex values fixed: {stats['bad_sex']}")
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise


# -- Transliteration -------------------------------------------------------

_REL_EN = {
    'പി': 'Father',
    'ഭ': 'Husband',
    'മാ': 'Mother',
    'മ': 'Mother',
    'ര': 'Other',
    'Null': '',
    '': '',
}

_SEX_EN = {'M': 'M', 'F': 'F'}


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


def _reorder_malayalam_matras(text):
    """Reorder detached pre-base matras (െ, േ, ൈ) to after their consonant.

    Malayalam pre-base matras are visually rendered before the consonant but
    stored after it in Unicode logical order.  Legacy PDF fonts sometimes
    emit them in visual order (matra before consonant), which breaks
    transliteration.  This moves them to the correct post-consonant position.
    """
    # Pre-base matras (െ U+0D46, േ U+0D47, ൈ U+0D48) followed by a
    # consonant (U+0D15-U+0D39) or chillu (U+0D7A-U+0D7F) -> swap
    text = re.sub(r'([\u0D46\u0D47\u0D48])([\u0D05-\u0D39\u0D7A-\u0D7F])', r'\2\1', text)
    # NFC normalization composes decomposed vowel signs:
    #   e-matra + aa-matra -> o-matra (U+0D4A)
    #   ee-matra + aa-matra -> oo-matra (U+0D4B)
    #   e-matra + au-length-mark -> au-matra (U+0D4C)
    text = unicodedata.normalize('NFC', text)
    return text


def _transliterate_malayalam(text):
    """Transliterate Malayalam text to approximate English via IAST."""
    if not text or not text.strip():
        return ''
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate
    t = text.strip()
    # Fix detached pre-base matras before transliteration
    t = _reorder_malayalam_matras(t)
    for chillu, repl in _CHILLU_MAP.items():
        t = t.replace(chillu, repl)
    iast = transliterate(t, sanscript.MALAYALAM, sanscript.IAST)
    diacritic_map = {
        'ā': 'a', 'ī': 'i', 'ū': 'u', 'ṛ': 'ri',
        'ś': 'sh', 'ṣ': 'sh', 'ṭ': 't', 'ḍ': 'd', 'ṇ': 'n',
        'ñ': 'n', 'ṅ': 'ng', 'ḥ': 'h', 'ṃ': 'm', 'ḻ': 'l', 'ṟ': 'r',
    }
    for k, v in diacritic_map.items():
        iast = iast.replace(k, v)
    # Malayalam: no schwa deletion
    return ' '.join(iast.split()).title()


def _transliterate_csv(csv_path):
    """Add English transliteration columns to an existing Lakshadweep CSV."""
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
                row['elector_name_en'] = _transliterate_malayalam(row.get('elector_name', ''))
                row['relation_name_en'] = _transliterate_malayalam(row.get('relation_name', ''))
                row['relation_type_en'] = _REL_EN.get(row.get('relation', '').strip(), '')
                row['sex_en'] = _SEX_EN.get(row.get('sex', '').strip(), '')
                writer.writerow(row)
                count += 1
                if count % 10_000 == 0:
                    print(f"  {count:,} rows transliterated...", flush=True)
        tmp.close()
        shutil.move(tmp.name, csv_path)
        print(f"Transliterated {csv_path}: {count:,} rows, added elector_name_en/relation_name_en/relation_type_en/sex_en")
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ac", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out-dir", default="output/csv/lakshadweep")
    ap.add_argument("--combined", action="store_true")
    ap.add_argument("--postprocess", default=None,
                    help="Path to existing CSV to postprocess (decode CIDs in-place)")
    ap.add_argument("--transliterate", metavar="CSV", default=None,
                    help="add English transliteration columns to an existing CSV")
    args = ap.parse_args()

    if args.transliterate:
        _transliterate_csv(args.transliterate)
        return

    if args.postprocess:
        postprocess_csv(args.postprocess, args.postprocess)
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
        ac_no = int(re.search(r"\d+", zf).group())
        ac_info = ac_map.get(ac_no, {})
        ac_name = ac_info.get("ac_name", "")
        district = ac_info.get("district_name", "")
        print(f"  {zf}: AC{ac_no:03d} {ac_name}...", end=" ", flush=True)

        with zipfile.ZipFile(os.path.join(raw_dir, zf)) as z:
            ac_rows = []
            for pf in sorted(n for n in z.namelist() if n.endswith(".pdf")):
                m = re.search(r"(\d+)", os.path.basename(pf))
                if not m: continue
                part_no = int(m.group(1))
                for row in extract_pdf_table(z.read(pf)):
                    # row: [dist, ac, island, part, serial, house, name, rel, rel_name, gender_l, sex, age, epic, status]
                    ac_rows.append([
                        STATE_ID, district, ac_no, ac_name, part_no,
                        row[4], row[5], row[6], row[7], row[8],
                        row[9], row[10], row[11],
                        row[12] if len(row) > 12 else "",
                        row[13] if len(row) > 13 else "",
                        ROLL_YEAR,
                    ])

        if args.combined:
            all_rows.extend(ac_rows)
        else:
            with open(os.path.join(args.out_dir, f"AC{ac_no:03d}.csv"), "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)
                csv.writer(f).writerows(ac_rows)
        print(f"{len(ac_rows)} rows")

    if args.combined:
        path = os.path.join(args.out_dir, f"{STATE_ID}_combined.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)
            csv.writer(f).writerows(all_rows)
        print(f"Combined: {path} ({len(all_rows)} rows)")


if __name__ == "__main__":
    main()
