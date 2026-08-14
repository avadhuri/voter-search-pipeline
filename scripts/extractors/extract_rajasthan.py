"""
Extract Rajasthan old SIR (2002) voter roll PDFs to CSV.

PDF has ruled tables extractable via pdfplumber.extract_tables().
However, all voter data in each table cell is crammed into a single cell
as newline-separated text. Each line contains:
    serial_no house_no elector_name relation_code relation_name sex age [epic_no]

Text is in Devanagari Unicode + DevLys legacy - extracted as-is (raw characters preserved).

Usage:
    python scripts/extractors/extract_rajasthan.py
    python scripts/extractors/extract_rajasthan.py --combined
"""
import argparse, csv, io, json, os, re, zipfile
import pdfplumber

STATE_ID = "rajasthan"
CSV_HEADERS = [
    "state", "district", "ac_no", "ac_name", "part_no",
    "serial_no", "house_no", "elector_name", "relation",
    "relation_name", "sex", "age", "epic_no",
]

# Devanagari keywords used in the rolls
_RELATION_CODES = {"वपतख", "पवत", "मखतख", "भ"}
_SEX_MALE = {"पयरष"}
_SEX_FEMALE = {"सध", "मतहलब"}
_ALL_SEX = _SEX_MALE | _SEX_FEMALE

# EPIC patterns: RJ/06/041/078071  or  MKV/0952465  or  bare 6+ digit
_EPIC_RE = re.compile(
    r"^(?:RJ/\d{2}/\d{3}/\d+|[A-Z]{2,4}/\d+|\d{6,})$"
)


def _load_meta():
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{STATE_ID}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def _parse_data_line(line):
    """Parse a single voter-data line into field dict, or None if unparseable.

    Expected token layout (left to right):
        serial_no  house_no  name_tokens...  relation_code  relname_tokens...  sex  age  [epic_no]

    We anchor on the Devanagari keywords (relation code, sex) to split the
    variable-width name/relation-name fields.
    """
    tokens = line.split()
    if len(tokens) < 6:
        return None

    # -- serial_no (first token, must be all digits) --
    if not tokens[0].isdigit():
        return None
    serial_no = tokens[0]

    # -- house_no (second token, digits possibly with dash) --
    house_no = tokens[1]
    if not re.match(r"^[\d\-]+$", house_no):
        return None

    # -- scan from the right for epic, age, sex --
    right = len(tokens)
    epic_no = ""

    # optional epic_no at the very end
    if _EPIC_RE.match(tokens[right - 1]):
        epic_no = tokens[right - 1]
        right -= 1

    if right < 5:
        return None

    # age: a 1-3 digit number just before (or at) the current right edge
    age_candidate = tokens[right - 1]
    if re.match(r"^\d{1,3}$", age_candidate):
        age = age_candidate
        right -= 1
    else:
        age = ""

    if right < 4:
        return None

    # sex: Devanagari gender keyword
    sex_token = tokens[right - 1]
    if sex_token in _ALL_SEX:
        sex = "M" if sex_token in _SEX_MALE else "F"
        right -= 1
    else:
        sex = ""

    if right < 4:
        return None

    # -- find the relation code scanning forward from token index 2 --
    rel_idx = None
    for i in range(2, right):
        if tokens[i] in _RELATION_CODES:
            rel_idx = i
            break

    if rel_idx is None:
        # No relation code found — still capture what we can
        elector_name = " ".join(tokens[2:right])
        return {
            "serial_no": serial_no,
            "house_no": house_no,
            "elector_name": elector_name,
            "relation": "",
            "relation_name": "",
            "sex": sex,
            "age": age,
            "epic_no": epic_no,
        }

    elector_name = " ".join(tokens[2:rel_idx])
    relation = tokens[rel_idx]
    relation_name = " ".join(tokens[rel_idx + 1:right])

    return {
        "serial_no": serial_no,
        "house_no": house_no,
        "elector_name": elector_name,
        "relation": relation,
        "relation_name": relation_name,
        "sex": sex,
        "age": age,
        "epic_no": epic_no,
    }


def extract_pdf_table(pdf_bytes):
    """Extract voter records from a Rajasthan PDF.

    Handles two layouts:
      1. Normal multi-column tables (>=7 columns per row).
      2. Single-cell tables where all voter lines are newline-separated
         inside one cell — the common Rajasthan format.
    """
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row:
                        continue

                    # --- Strategy 1: proper multi-column row ---
                    if len(row) >= 7:
                        sl = (row[0] or "").strip()
                        if sl and sl.isdigit():
                            cleaned = [str(c or "").strip().replace("\n", " ") for c in row]
                            rows.append(cleaned)
                            continue

                    # --- Strategy 2: newline-crammed single cell ---
                    # Check every cell for embedded newlines containing
                    # voter data lines (starts with a digit = serial no).
                    for cell in row:
                        if not cell:
                            continue
                        cell_text = str(cell).strip()
                        if "\n" not in cell_text:
                            # single-line cell — try parsing it directly
                            parsed = _parse_data_line(cell_text)
                            if parsed:
                                rows.append([
                                    parsed["serial_no"],
                                    parsed["house_no"],
                                    parsed["elector_name"],
                                    parsed["relation"],
                                    parsed["relation_name"],
                                    parsed["sex"],
                                    parsed["age"],
                                    parsed["epic_no"],
                                ])
                            continue

                        # multi-line cell: split and parse each line
                        for line in cell_text.split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            parsed = _parse_data_line(line)
                            if parsed:
                                rows.append([
                                    parsed["serial_no"],
                                    parsed["house_no"],
                                    parsed["elector_name"],
                                    parsed["relation"],
                                    parsed["relation_name"],
                                    parsed["sex"],
                                    parsed["age"],
                                    parsed["epic_no"],
                                ])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ac", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out-dir", default="output/csv/rajasthan")
    ap.add_argument("--combined", action="store_true")
    args = ap.parse_args()

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
        print(f"  {zf}...", end=" ", flush=True)

        with zipfile.ZipFile(os.path.join(raw_dir, zf)) as z:
            ac_rows = []
            for pf in sorted(n for n in z.namelist() if n.endswith(".pdf")):
                m = re.search(r"(\d+)", os.path.basename(pf))
                if not m: continue
                part_no = int(m.group(1))
                for row in extract_pdf_table(z.read(pf)):
                    r = (row + [""] * 13)[:13]
                    ac_rows.append([
                        STATE_ID, district, ac_no, ac_name, part_no,
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
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
