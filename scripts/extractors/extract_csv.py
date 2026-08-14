"""
Extract voter data from downloaded SIR PDFs to CSV.

Reads ZIPs from data/raw/{state_id}/ and extracts tabular data from each
part PDF using pdfplumber, outputting one CSV per AC.

Supports multiple PDF layouts (auto-detected per state):
  - layout_8col: 8-column English layout (Arunachal, Goa, Mizoram, Nagaland,
                 Sikkim, A&N, Delhi, and similar)
  - More layouts to be added as states are analyzed.

Usage:
    # Extract one state
    python scripts/extract_csv.py --state sikkim

    # Extract specific ACs
    python scripts/extract_csv.py --state goa --ac 1,2

    # Extract first N ACs (testing)
    python scripts/extract_csv.py --state delhi --limit 3

    # Custom output directory
    python scripts/extract_csv.py --state sikkim --out-dir output/csv
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

META_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "states", "meta")

# State -> layout type
STATE_LAYOUTS = {
    # Layout A: 8 columns (Serial, House, Name, Relation, Relative, Sex, Age, EPIC)
    "arunachal_pradesh": "layout_8col",
    "goa": "layout_8col",
    "mizoram": "layout_8col",
    "nagaland": "layout_8col",
    "sikkim": "layout_8col",
    "andaman_nicobar": "layout_8col",
    "delhi": "layout_8col",
}

ROW_TOL = 5.0  # pt: cluster words into rows


def _group_into_rows(words, tolerance=ROW_TOL):
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


def _find_column_row(words, n_cols_min=6):
    """Find the '1 2 3 ... N' or '(1) (2) (3) ...' column-number row."""
    if not words:
        return None
    # Try bare digits first
    singles = [w for w in words if re.fullmatch(r"\d", w["text"])]
    if len(singles) >= n_cols_min:
        singles.sort(key=lambda w: w["top"])
        for i in range(len(singles)):
            group = [singles[i]]
            for j in range(i + 1, len(singles)):
                if abs(singles[j]["top"] - singles[i]["top"]) <= ROW_TOL:
                    group.append(singles[j])
            labels = sorted(group, key=lambda w: w["x0"])
            nums = [w["text"] for w in labels]
            if len(nums) >= n_cols_min and nums[0] == "1" and nums[1] == "2":
                return {int(w["text"]): (w["x0"] + w["x1"]) / 2 for w in labels}
    # Try (1) (2) format
    parens = [w for w in words if re.fullmatch(r"\(\d\)", w["text"])]
    if len(parens) >= n_cols_min:
        parens.sort(key=lambda w: w["top"])
        for i in range(len(parens)):
            group = [parens[i]]
            for j in range(i + 1, len(parens)):
                if abs(parens[j]["top"] - parens[i]["top"]) <= ROW_TOL:
                    group.append(parens[j])
            labels = sorted(group, key=lambda w: w["x0"])
            nums = [re.search(r"\d", w["text"]).group() for w in labels]
            if len(nums) >= n_cols_min and nums[0] == "1" and nums[1] == "2":
                return {int(re.search(r"\d", w["text"]).group()): (w["x0"] + w["x1"]) / 2 for w in labels}
    return None


def _make_boundaries(col_centres, narrow_cols=None):
    """Convert column centres to boundary ranges (left_edge, right_edge) per column.

    narrow_cols: set of column numbers known to be narrow (e.g. relation code,
    sex, age). For these, the boundary is biased towards the column centre
    (20% of gap to previous, 20% of gap to next) so wide neighbour columns
    don't steal their content.
    """
    if narrow_cols is None:
        narrow_cols = set()
    cols = sorted(col_centres.keys())
    boundaries = {}
    for i, col in enumerate(cols):
        if i == 0:
            left = 0
        else:
            gap = col_centres[col] - col_centres[cols[i - 1]]
            if col in narrow_cols:
                left = col_centres[col] - gap * 0.2
            elif cols[i - 1] in narrow_cols:
                left = col_centres[cols[i - 1]] + gap * 0.2
            else:
                left = col_centres[cols[i - 1]] + gap * 0.5
        if i == len(cols) - 1:
            right = 9999
        else:
            gap = col_centres[cols[i + 1]] - col_centres[col]
            if col in narrow_cols:
                right = col_centres[col] + gap * 0.2
            elif cols[i + 1] in narrow_cols:
                right = col_centres[cols[i + 1]] - gap * 0.2
            else:
                right = col_centres[col] + gap * 0.5
        boundaries[col] = (left, right)
    return boundaries


def _assign_column(x_centre, col_centres, boundaries=None):
    if boundaries:
        for col, (left, right) in boundaries.items():
            if left <= x_centre < right:
                return col
    # Fallback: nearest centre
    best_col, best_dist = min(col_centres.keys()), float("inf")
    for col, cx in col_centres.items():
        dist = abs(x_centre - cx)
        if dist < best_dist:
            best_col, best_dist = col, dist
    return best_col


def _row_cells(row_words, col_centres, n_cols, narrow_cols=None):
    boundaries = _make_boundaries(col_centres, narrow_cols)
    buckets = {c: [] for c in col_centres}
    for w in row_words:
        centre = (w["x0"] + w["x1"]) / 2
        col = _assign_column(centre, col_centres, boundaries)
        buckets.setdefault(col, []).append(w)
    cells = []
    for c in sorted(col_centres.keys()):
        parts = sorted(buckets.get(c, []), key=lambda w: w["x0"])
        cells.append(" ".join(w["text"] for w in parts).strip())
    return cells


# ── Layout: 8-column English ────────────────────────────────────────────
# Columns: Serial, House, Name, Relation, Relative Name, Sex, Age, EPIC
COL_NAMES_8 = ["serial_no", "house_no", "elector_name", "relation", "relation_name", "sex", "age", "epic_no"]


def extract_8col_page(page, fallback_centres=None):
    """Extract voter rows from one page of an 8-column English PDF."""
    words = page.extract_words()
    if not words:
        return [], fallback_centres

    col_centres = _find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None

    n_cols = max(col_centres.keys())

    # Find y of column-number row to skip header
    col_row_top = None
    for w in words:
        if re.fullmatch(r"[\d()]", w["text"][:1]):
            centre = (w["x0"] + w["x1"]) / 2
            for col, cx in col_centres.items():
                if abs(centre - cx) < 10 and str(col) in w["text"]:
                    if col_row_top is None or w["top"] < col_row_top:
                        col_row_top = w["top"]
                    break

    if col_row_top is None:
        col_row_top = 0

    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    rows = _group_into_rows(data_words)

    n_cols = len(col_centres)
    # Columns 4 (relation code), 6 (sex), 7 (age) are narrow single-value columns
    narrow = {4, 6, 7} if n_cols == 8 else set()
    records = []
    for row_words in rows:
        cells = _row_cells(row_words, col_centres, n_cols, narrow_cols=narrow)
        serial = cells[0].strip()
        # A data row starts with a numeric serial number
        if serial and re.fullmatch(r"\d+", serial):
            # Pad/trim to 8 columns for consistency
            cells = (cells + [""] * 8)[:8]
            records.append(cells)

    return records, col_centres


def extract_pdf_8col(pdf_bytes):
    """Extract all voter rows from an 8-column PDF."""
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, col_centres = extract_8col_page(page, col_centres)
            all_rows.extend(rows)
    return all_rows


# ── Main extraction logic ───────────────────────────────────────────────

def extract_ac_zip(zip_path, layout):
    """Extract all voter rows from an AC's ZIP of part PDFs."""
    all_rows = []
    with zipfile.ZipFile(zip_path) as zf:
        # Read manifest for metadata
        manifest = {}
        if "manifest.json" in zf.namelist():
            manifest = json.loads(zf.read("manifest.json"))

        part_files = sorted([n for n in zf.namelist() if n.endswith(".pdf")])
        for part_file in part_files:
            m = re.search(r"(\d+)", os.path.basename(part_file))
            if not m:
                continue
            part_no = int(m.group(1))
            pdf_bytes = zf.read(part_file)

            if layout == "layout_8col":
                rows = extract_pdf_8col(pdf_bytes)
            else:
                print(f"    SKIP {part_file}: unknown layout '{layout}'")
                continue

            for cells in rows:
                all_rows.append([part_no] + cells)

    return all_rows, manifest


def load_meta(state_id):
    meta_file = os.path.join(META_DIR, f"{state_id}_ac_meta.json")
    if not os.path.exists(meta_file):
        raise FileNotFoundError(f"Meta not found: {meta_file}")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Extract voter data from SIR PDFs to CSV")
    ap.add_argument("--state", required=True, help="state_id (e.g. sikkim, goa)")
    ap.add_argument("--ac", default=None, help="AC numbers, comma-separated (e.g. 1,2,3)")
    ap.add_argument("--limit", type=int, default=None, help="only process first N ACs")
    ap.add_argument("--out-dir", default="output/csv", help="output directory")
    ap.add_argument("--combined", action="store_true", help="output single CSV for entire state")
    args = ap.parse_args()

    state_id = args.state
    if state_id not in STATE_LAYOUTS:
        print(f"Unsupported state: {state_id}")
        print(f"Supported: {', '.join(sorted(STATE_LAYOUTS.keys()))}")
        sys.exit(1)

    layout = STATE_LAYOUTS[state_id]
    raw_dir = os.path.join("data", "raw", state_id)
    out_dir = os.path.join(args.out_dir, state_id)
    os.makedirs(out_dir, exist_ok=True)

    meta = load_meta(state_id)
    ac_map = {ac["ac_no"]: ac for ac in meta}

    # Filter ACs
    zip_files = sorted([f for f in os.listdir(raw_dir) if f.endswith(".zip")])
    if args.ac:
        wanted = set(int(x) for x in args.ac.split(","))
        zip_files = [f for f in zip_files if int(re.search(r"\d+", f).group()) in wanted]
    if args.limit:
        zip_files = zip_files[:args.limit]

    print(f"\n{state_id}: extracting {len(zip_files)} ACs using {layout}")

    if layout == "layout_8col":
        col_headers = ["part_no"] + COL_NAMES_8
    else:
        col_headers = ["part_no", "col1", "col2", "col3", "col4", "col5", "col6", "col7", "col8"]

    # Add state/district/ac metadata columns
    full_headers = ["state", "district", "ac_no", "ac_name"] + col_headers

    all_state_rows = []

    for zip_file in zip_files:
        zip_path = os.path.join(raw_dir, zip_file)
        ac_no = int(re.search(r"\d+", zip_file).group())
        ac_info = ac_map.get(ac_no, {})
        ac_name = ac_info.get("ac_name", "")
        district = ac_info.get("district_name", "")

        print(f"  {zip_file}: AC{ac_no:03d} {ac_name} ({district})...", end=" ", flush=True)

        rows, manifest = extract_ac_zip(zip_path, layout)

        # Prepend metadata
        full_rows = []
        for row in rows:
            full_rows.append([state_id, district, ac_no, ac_name] + row)

        if args.combined:
            all_state_rows.extend(full_rows)
        else:
            # Write per-AC CSV
            csv_path = os.path.join(out_dir, f"AC{ac_no:03d}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(full_headers)
                writer.writerows(full_rows)

        print(f"{len(rows)} rows")

    if args.combined:
        csv_path = os.path.join(out_dir, f"{state_id}_combined.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(full_headers)
            writer.writerows(all_state_rows)
        print(f"\nCombined CSV: {csv_path} ({len(all_state_rows)} rows)")
    else:
        print(f"\nPer-AC CSVs saved to {out_dir}/")

    total = sum(1 for _ in open(os.path.join(out_dir, os.listdir(out_dir)[0]))) - 1 if not args.combined else len(all_state_rows)
    print(f"Total rows extracted: {len(all_state_rows) if args.combined else 'see per-AC files'}")


if __name__ == "__main__":
    main()
