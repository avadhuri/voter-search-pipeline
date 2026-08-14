"""
Shared PDF extraction utilities used by per-state extract_*.py scripts.

Provides:
  - Column detection from header rows ((1)(2)...(N) or bare 1 2...N)
  - Word grouping into rows by y-position
  - Column assignment with narrow-column awareness
  - ZIP handling (read part PDFs from AC ZIPs)
"""
import io
import json
import os
import re
import zipfile

import pdfplumber

ROW_TOL = 5.0  # pt: cluster words into rows


def group_into_rows(words, tolerance=ROW_TOL):
    """Group words by approximate y position into rows."""
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


def find_column_row(words, min_cols=6):
    """Find the column-number header row: (1)(2)...(N) or bare 1 2 3...N.
    Returns {col_num: x_centre} or None."""
    if not words:
        return None

    # Try (1)(2) format first
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

    # Try bare digit format
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


def make_boundaries(col_centres, narrow_cols=None):
    """Convert column centres to (left, right) boundary ranges.

    narrow_cols: set of column numbers known to be narrow (e.g. relation code,
    sex, age). Boundary is biased 20/80 instead of 50/50 so neighbours don't
    steal their content.
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


def assign_column(x_centre, col_centres, boundaries):
    """Assign an x position to a column using boundaries."""
    for col, (left, right) in boundaries.items():
        if left <= x_centre < right:
            return col
    # Fallback: nearest centre
    best_col = min(col_centres, key=lambda c: abs(col_centres[c] - x_centre))
    return best_col


def row_to_cells(row_words, col_centres, boundaries):
    """Split row words into column buckets and join text per column."""
    buckets = {c: [] for c in col_centres}
    for w in row_words:
        cx = (w["x0"] + w["x1"]) / 2
        col = assign_column(cx, col_centres, boundaries)
        buckets.setdefault(col, []).append(w)
    cells = []
    for c in sorted(col_centres.keys()):
        parts = sorted(buckets.get(c, []), key=lambda w: w["x0"])
        cells.append(" ".join(w["text"] for w in parts).strip())
    return cells


def extract_page(page, n_cols, narrow_cols=None, fallback_centres=None):
    """Extract data rows from one PDF page.

    Returns (list_of_cell_lists, col_centres_for_next_page).
    Each cell list has n_cols entries, padded/trimmed.
    Only rows starting with a numeric serial number are returned.
    """
    words = page.extract_words()
    if not words:
        return [], fallback_centres

    col_centres = find_column_row(words) or fallback_centres
    if col_centres is None:
        return [], None

    boundaries = make_boundaries(col_centres, narrow_cols)

    # Find y of column-number row to skip headers
    col_row_top = 0
    for w in words:
        txt = w["text"]
        if re.fullmatch(r"\(?\d\)?", txt):
            cx = (w["x0"] + w["x1"]) / 2
            digit = int(re.search(r"\d", txt).group())
            if digit in col_centres and abs(cx - col_centres[digit]) < 15:
                col_row_top = max(col_row_top, w["top"])

    data_words = [w for w in words if w["top"] > col_row_top + ROW_TOL]
    rows = group_into_rows(data_words)

    records = []
    for rw in rows:
        cells = row_to_cells(rw, col_centres, boundaries)
        serial = cells[0].strip() if cells else ""
        if serial and re.fullmatch(r"\d+", serial):
            cells = (cells + [""] * n_cols)[:n_cols]
            records.append(cells)

    return records, col_centres


def extract_pdf(pdf_bytes, n_cols, narrow_cols=None):
    """Extract all data rows from a PDF. Returns list of cell lists."""
    all_rows = []
    col_centres = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            rows, col_centres = extract_page(page, n_cols, narrow_cols, col_centres)
            all_rows.extend(rows)
    return all_rows


def extract_ac_zip(zip_path, n_cols, narrow_cols=None):
    """Extract all voter rows from an AC's ZIP of part PDFs.
    Returns (list_of_rows_with_part_no_prepended, manifest_dict)."""
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
            rows = extract_pdf(zf.read(pf), n_cols, narrow_cols)
            for cells in rows:
                all_rows.append([part_no] + cells)
    return all_rows, manifest


def load_meta(state_id):
    """Load states/meta/{state_id}_ac_meta.json."""
    meta_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "states", "meta")
    meta_file = os.path.join(meta_dir, f"{state_id}_ac_meta.json")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)
