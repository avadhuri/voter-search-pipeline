"""
Generic CSV-based StateConnector for pre-extracted state data.

Reads per-AC CSV files produced by scripts/split_csv.py (one file per AC,
split from the combined CSVs that scripts/extractors/extract_*.py output).
Maps the various column-naming conventions across 19 states to VoterRecord's
common shape via a single alias dict — no per-state code needed.

Column variations handled:
  - ac_no / ac_number → ac_code
  - part_no / part_number → part_no
  - serial_no / serial_number → serial_no
  - relation / relation_type → relation_code (mapped to F/H/M/O)
  - elector_name_en preferred over elector_name for full_name
    (pre-transliterated; for English states they're the same)
  - relation_name_en preferred over relation_name
  - relation_type_en preferred for relation_code mapping

States missing columns (e.g. Tripura has no state/district/ac_name/house_no)
are filled from the ac_meta.json via list_constituencies().
"""
import csv
import io
import json
import os

from states.base import Constituency, StateConnector, VoterRecord

# Maps relation labels (English) to VoterRecord's F/H/M/O codes
_REL_TO_CODE = {
    "father": "F", "f": "F",
    "husband": "H", "h": "H",
    "mother": "M", "m": "M",
    "other": "O", "o": "O", "guardian": "O",
}


def _make_csv_connector_cls(state_id):
    """Factory that returns a picklable CsvConnector subclass for a state.

    build_db.py's --per-ac mode pickles connector_cls to send to worker
    processes (ProcessPoolExecutor).  Lambdas and closures can't be pickled,
    but a module-level class can — so we create one subclass per state at
    import time and store it in the registry.
    """
    cls = type(
        f"CsvConnector_{state_id}",
        (CsvConnector,),
        {"_default_state_id": state_id},
    )
    # Register in this module's namespace so pickle can find it
    globals()[cls.__name__] = cls
    return cls


class CsvConnector(StateConnector):
    """Reads pre-extracted per-AC CSV files and returns VoterRecords."""

    _default_state_id = None  # overridden by _make_csv_connector_cls subclasses

    def __init__(self, state_id=None, meta_file=None):
        self.state_id = state_id or self._default_state_id
        self._meta_file = meta_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "meta", f"{self.state_id}_ac_meta.json")
        self._meta = None

    def _load_meta(self):
        if self._meta is None:
            with open(self._meta_file, encoding="utf-8") as f:
                self._meta = json.load(f)
        return self._meta

    def list_constituencies(self):
        meta = self._load_meta()
        return [
            Constituency(
                ac_code=str(ac["ac_no"]),
                ac_name=ac.get("ac_name", ""),
                district=ac.get("district_name", ""),
                total_parts=ac.get("total_parts", 0),
            )
            for ac in meta
        ]

    def fetch_raw(self, ac, roll_year):
        # Not used — build_db.py reads the file directly and passes bytes
        # to parse_raw. This exists only to satisfy the interface.
        raise NotImplementedError(
            "CsvConnector.fetch_raw() is not used — build_db.py reads "
            "per-AC CSV files directly from raw_dir/raw_glob.")

    def parse_raw(self, raw, ac, roll_year):
        """Parse a per-AC CSV file (as bytes) into VoterRecords.

        Detects columns from the CSV header and maps them to VoterRecord
        fields.  For non-Latin states with _en columns, uses the English
        transliteration as full_name (searchable).  For English states,
        elector_name is already Latin.
        """
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        fields = set(reader.fieldnames or [])

        # Detect which column names this CSV uses
        has_en = "elector_name_en" in fields
        has_rel_en = "relation_type_en" in fields
        ac_col = "ac_number" if "ac_number" in fields else "ac_no"
        part_col = "part_number" if "part_number" in fields else "part_no"
        serial_col = "serial_number" if "serial_number" in fields else "serial_no"
        rel_col = "relation_type" if "relation_type" in fields else "relation"

        records = []
        for row in reader:
            # --- Name: prefer _en (pre-transliterated) ---
            if has_en:
                name = (row.get("elector_name_en") or "").strip()
                rel_name = (row.get("relation_name_en") or "").strip()
                # Fall back to native if _en is empty
                if not name:
                    name = (row.get("elector_name") or "").strip()
                if not rel_name:
                    rel_name = (row.get("relation_name") or "").strip()
            else:
                name = (row.get("elector_name") or "").strip()
                rel_name = (row.get("relation_name") or "").strip()

            # --- Relation code: prefer _en, map to F/H/M/O ---
            if has_rel_en:
                rel_raw = (row.get("relation_type_en") or "").strip()
            else:
                rel_raw = (row.get(rel_col) or "").strip()
            relation_code = _REL_TO_CODE.get(rel_raw.lower(), rel_raw.upper()[:1])

            # --- Age: take first number, handle empty ---
            age_raw = (row.get("age") or "").strip()
            try:
                age = int(age_raw)
            except (ValueError, TypeError):
                age = 0

            # --- Gender ---
            gender = (row.get("sex") or "").strip().upper()
            if gender not in ("M", "F"):
                gender = ""

            # --- Fields that may be missing (Tripura) ---
            state = (row.get("state") or self.state_id).strip()
            district = (row.get("district") or ac.district).strip()
            ac_name = (row.get("ac_name") or ac.ac_name).strip()
            house_no = (row.get("house_no") or "").strip()
            locality = (row.get("locality") or "").strip()

            # --- Part/serial ---
            try:
                part_no = int(row.get(part_col) or 0)
            except (ValueError, TypeError):
                part_no = 0
            try:
                serial_no = int(row.get(serial_col) or 0)
            except (ValueError, TypeError):
                serial_no = 0

            records.append(VoterRecord(
                state=state,
                district=district,
                ac_code=str(ac.ac_code),
                ac_name=ac_name,
                part_no=part_no,
                serial_no=serial_no,
                local_ref=house_no,
                full_name=name,
                full_relative_name=rel_name,
                relation_code=relation_code,
                age=age,
                gender=gender,
                roll_year=roll_year,
                locality=locality,
            ))

        return records
