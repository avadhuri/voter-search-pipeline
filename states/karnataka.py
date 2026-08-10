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

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AC_META_PATH = os.path.join(_HERE, "data", "ac_meta.json")

CSV_URL_TEMPLATE = "https://ceo.karnataka.gov.in/csv_upload/english/{ac_code}.csv"

RELATION_LABELS = {
    "F": "Father",
    "H": "Husband",
    "M": "Mother",
    "O": "Other/Guardian",
}


def _clean(val):
    val = (val or "").strip()
    return "" if val.upper() == "NULL" else val


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
        text = raw.decode("utf-8-sig", errors="replace")
        records = []
        reader = csv.reader(io.StringIO(text))
        for cols in reader:
            if len(cols) != 13:
                continue
            (
                district, ac_code, ac_name, part_no, serial_no, local_ref,
                first_name, first_suffix, relative_name, relative_suffix,
                relation_code, age, gender,
            ) = [_clean(c) for c in cols]

            full_name = " ".join(p for p in (first_name, first_suffix) if p)
            full_relative_name = " ".join(
                p for p in (relative_name, relative_suffix) if p
            )

            try:
                part_no_i = int(part_no) if part_no else None
                serial_no_i = int(serial_no) if serial_no else None
                age_i = int(age) if age else None
            except ValueError:
                continue

            records.append(VoterRecord(
                state=self.state_id,
                district=district,
                ac_code=ac_code,
                ac_name=ac_name,
                part_no=part_no_i,
                serial_no=serial_no_i,
                local_ref=local_ref,
                full_name=full_name,
                full_relative_name=full_relative_name,
                relation_code=relation_code,
                age=age_i,
                gender=gender,
                roll_year=roll_year,
            ))
        return records
