"""
Karnataka connector: ceo.karnataka.gov.in's 2002 roll.

Data source, reverse-engineered from the CEO site's own client-side JS
(data/page_script.js, fetchAndSearchCSV): every AC's fully digitized 2002
roll is available as a flat CSV at a predictable URL, with no CAPTCHA or
auth on the file itself (the CAPTCHA on voter_list.html only gates the
site's own search UI, not this endpoint).

Raw CSV column layout (0-indexed, no header), see also build_db.py:
  0  district
  1  ac_code            e.g. "A085"
  2  ac_name
  3  part_no
  4  serial_no
  5  local_ref
  6  first_name
  7  first_name_suffix
  8  relative_name
  9  relative_name_suffix
  10 relation_code      F=Father, H=Husband, M=Mother, O=Other/Guardian
  11 age
  12 gender
"""
import csv
import io
import json
import os

import requests

from states.base import Constituency, StateConnector, VoterRecord

_HERE = os.path.dirname(os.path.abspath(__file__))
AC_META_PATH = os.path.join(_HERE, "meta", "ac_meta.json")

CSV_URL_TEMPLATE = "https://ceo.karnataka.gov.in/csv_upload/english/{ac_code}.csv"

RELATION_LABELS = {
    "F": "Father",
    "H": "Husband",
    "M": "Mother",
    "O": "Other/Guardian",
}

# Confidently-recognized relation_code variants, gathered from a full audit of
# every downloaded AC's raw CSV (scripts/audit_raw_csv.py) -- the source data
# mixes English full words, English single letters, and Kannada
# transliterations (sometimes truncated/misspelled) with no single convention
# per file. Only variants observed with clear, unambiguous meaning are
# normalized silently; anything else is left as the raw value with a remark
# rather than guessed at (e.g. "WIFE"/"WO"/"HEMDATI" denote a spouse relation
# that doesn't fit the father/husband/mother/other scheme at all).
RELATION_NORMALIZE = {
    "": "", "-": "",
    "F": "F", "FATHER": "F", "TAMDE": "F", "TAMDA": "F", "TAMDE.": "F", "TEMDE": "F", "S/O": "F",
    "H": "H", "HUSBAND": "H", "GAMDA": "H", "GAMDE": "H",
    "M": "M", "MOTHER": "M", "TAYI": "M",
    "O": "O", "OTHER": "O", "ITARE": "O", "ITERE": "O",
}

# Same story for gender -- MALE/FEMALE, M/F, and Kannada-transliteration
# fragments (GAN/HEM/HAN) all appear across different ACs' files.
GENDER_NORMALIZE = {
    "": "", "-": "",
    "MALE": "M", "M": "M", "GAN": "M",
    "FEMALE": "F", "F": "F", "HEM": "F", "HAN": "F",
}


def _clean(val):
    val = (val or "").strip()
    return "" if val.upper() == "NULL" else val


def _normalize(raw, table, field_label, remarks):
    """Look up raw (case/whitespace-insensitively) in table; if not found,
    keep the original raw value as-is and note it in remarks rather than
    guessing at a mapping or silently dropping the information."""
    val = (raw or "").strip()
    key = val.upper()
    if key in table:
        return table[key]
    if val:
        remarks.append(f"unrecognized {field_label}: {val!r}")
    return val


def _parse_int(raw, field_label, remarks):
    """Blank/NULL is a normal 'not recorded' state (no remark). Anything
    else that fails to parse as an integer is a genuine data quirk -- kept
    as a null field with a remark rather than silently dropping the row."""
    val = (raw or "").strip()
    if not val or val.upper() == "NULL":
        return None
    try:
        return int(val)
    except ValueError:
        remarks.append(f"non-numeric {field_label}: {val!r}")
        return None


class KarnatakaConnector(StateConnector):
    state_id = "karnataka"

    def list_constituencies(self) -> list:
        with open(AC_META_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return [
            Constituency(
                ac_code=row["ac_code"],
                ac_name=row["ac_name"],
                district=row["district"],
                total_parts=row.get("total_parts", 0),
                extra={"id": row.get("id")},
            )
            for row in raw
        ]

    def fetch_raw(self, ac: Constituency, roll_year: int) -> bytes:
        if roll_year != 2002:
            raise NotImplementedError(
                "Karnataka connector currently only implements the 2002 roll "
                "(csv_upload endpoint); the current/2025-26 roll needs the "
                "ECI CAPTCHA-gated pipeline (see scripts/download_2025_pilot.py)."
            )
        url = CSV_URL_TEMPLATE.format(ac_code=ac.ac_code)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def parse_raw(self, raw: bytes, ac: Constituency, roll_year: int) -> list:
        """
        Every row is kept -- nothing is silently dropped. district/ac_code/
        ac_name are trusted from `ac` (we already know which AC's file this
        is, since it's fetched per-AC) rather than the row's own copy of
        those fields, which sometimes disagrees in isolated rows (e.g. a
        handful of A083 rows say ac_code "83" instead of "A083") or uses a
        different spelling convention file-wide (e.g. "RAICHURU" vs the
        canonical "RAICHUR" -- consistent within a file, so not a per-row
        remark, just a silent normalization). Any row-level disagreement
        with the expected ac_code, or an unrecognized relation_code/gender/
        numeric field, is recorded in `remark` instead of being guessed at
        or discarded.
        """
        text = raw.decode("utf-8-sig", errors="replace")
        records = []
        reader = csv.reader(io.StringIO(text))
        for cols in reader:
            if not cols or all(not c.strip() for c in cols):
                continue  # blank line, not a data row

            remarks = []

            # Almost every AC's CSV has exactly 13 columns, but a few (e.g.
            # A030) ship with extra trailing empty columns, and none observed
            # in a full audit ship fewer than 13 -- pad defensively anyway so
            # a short row is kept (with a remark) rather than dropped.
            if len(cols) < 13:
                remarks.append(f"malformed row: only {len(cols)} columns (expected 13)")
                cols = cols + [""] * (13 - len(cols))
            elif len(cols) > 13 and any(c.strip() for c in cols[13:]):
                remarks.append(f"malformed row: {len(cols)} columns with non-empty trailing data")
            cols = cols[:13]

            (
                raw_district, raw_ac_code, raw_ac_name, part_no, serial_no, local_ref,
                first_name, first_suffix, relative_name, relative_suffix,
                relation_code, age, gender,
            ) = [_clean(c) for c in cols]

            norm_ac_code = raw_ac_code.upper()
            if norm_ac_code and not norm_ac_code.startswith("A"):
                norm_ac_code = "A" + norm_ac_code.zfill(3)
            if norm_ac_code != ac.ac_code:
                remarks.append(f"source row listed ac_code {raw_ac_code!r}; using {ac.ac_code} (from filename)")

            full_name = " ".join(p for p in (first_name, first_suffix) if p)
            full_relative_name = " ".join(
                p for p in (relative_name, relative_suffix) if p
            )

            part_no_i = _parse_int(part_no, "part_no", remarks)
            serial_no_i = _parse_int(serial_no, "serial_no", remarks)
            age_i = _parse_int(age, "age", remarks)
            relation_code = _normalize(relation_code, RELATION_NORMALIZE, "relation_code", remarks)
            gender = _normalize(gender, GENDER_NORMALIZE, "gender", remarks)

            records.append(VoterRecord(
                state=self.state_id,
                district=ac.district or raw_district,
                ac_code=ac.ac_code,
                ac_name=ac.ac_name or raw_ac_name,
                part_no=part_no_i,
                serial_no=serial_no_i,
                local_ref=local_ref,
                full_name=full_name,
                full_relative_name=full_relative_name,
                relation_code=relation_code,
                age=age_i,
                gender=gender,
                roll_year=roll_year,
                remark="; ".join(remarks),
            ))
        return records
