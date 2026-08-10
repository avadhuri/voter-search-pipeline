"""
Haryana connector: ceoharyana.gov.in's 2002 roll (Intensive Revision).

DATA SOURCE
-----------
Reverse-engineered from the CEO site's own page at /WebCMS/Start/1939
("Searchable PDFs of Intensive Revision of Electoral Rolls 2002"). Three
unauthenticated JSON endpoints enumerate the hierarchy, and each part's PDF
sits at a static path:

  POST /WebCMS/GetDistrict2002                              YearID=2002
  POST /WebCMS/GetAssemblyConstituencyListByDistrictId2002  DistrictId=<n>
  POST /WebCMS/GetPartNameListByConstitutionId              ConstitutionId=<n>&YearID=2002
  GET  /ElectoralRoll2002/CMB<AC:02d>Year2002/CMB0<AC:02d><PART:04d>.pdf

The page shows a CAPTCHA, but it is client-side decoration only: its
ValidateForm() checks merely that the input is non-empty and never validates
the value, and GetFilePath() then builds the URL and window.open()s it. All
of the above was verified with plain unauthenticated requests -- no cookies,
no session, no CAPTCHA solving.

ONE PDF PER PART, ONE BLOB PER AC
--------------------------------
Unlike Karnataka (one CSV per AC), Haryana publishes one PDF per *part*, and
an AC has up to ~250 of them. To keep the pipeline's "one raw artifact per
AC" convention, fetch_raw() downloads an AC's parts and returns them bundled
as a single in-memory ZIP (entries named "part0001.pdf"), which parse_raw()
unzips and walks. Nothing outside this module needs to know.

The part list is fetched live in fetch_raw() rather than cached in
haryana_ac_meta.json. That is one extra POST amortised over ~200 PDF
downloads, and it removes a whole class of silent staleness: a cached list of
~15,000 part numbers that disagrees with the site would skip real parts
without anyone noticing. The meta file therefore carries only total_parts
(for progress reporting and as a sanity check against the live list).

TEXT LAYER: LEGACY 8-BIT FONT, AND ONLY ON HALF THE STATE
---------------------------------------------------------
Probing part 1 of all 90 ACs shows the collection is NOT uniform:

  * 44 ACs have a real, embedded text layer, but the text is NOT Unicode --
    it is 8-bit "DK-RAJ,Bold" legacy-font text with no /ToUnicode CMap. See
    states/haryana_dkraj.py for the transcoder and how it was validated.
    (41 pure DK-RAJ; 3 -- ACs 2, 31, 35 -- are hybrids that additionally
    carry the relation/gender columns in real Unicode via ArialUnicodeMS.)
  * 46 ACs are scans with no usable text: 41 carry a Tesseract
    "GlyphLessFont" invisible OCR layer over a page image whose output is
    unusable character-soup, 1 uses "HiddenHorzOCR", 3 (ACs 18, 38, 59) have
    no text layer at all, and AC 72 uses an unidentified "Untitled" font.

roll_format in haryana_ac_meta.json records which is which. parse_raw()
refuses to guess at a scanned AC: it raises UnparseableRollError rather than
returning an empty list that would look like a successful parse of an empty
roll. Those 46 ACs need an OCR pipeline, which is out of scope here.

TABLE LAYOUT
------------
Page 1 of each part PDF is a cover/summary page. Every subsequent page
carries an 8-column ruled table whose columns are recovered from the PDF's
own vertical ruling lines, keyed to the "[1]".."[8]" header markers printed
on each page, so nothing is hardcoded to a fixed x-offset:

  [1] क्रम संख्या        serial no
  [2] मकान संख्या        house no          -> local_ref (matches Karnataka's)
  [3] मतदाता का नाम      voter name
  [4] रिश्ता             relation code
  [5] रिश्तेदार का नाम   relative's name
  [6] लिंग               gender
  [7] आयु                age (as on 1.1.2002)
  [8] पहचान पत्र क्रम संख्या   EPIC number, e.g. "HR/07/61/0000002"

NOTE: VoterRecord has no field for the EPIC number, so it is currently
parsed and validated but not emitted. It is the only identifier that
persists across roll years and would be the natural join key for this
project's "search old, match new" feature -- worth a base.py field, which is
the lead's call, not this connector's.
"""
import io
import json
import os
import re
import time
import zipfile

import pdfplumber
import requests

from states.base import Constituency, StateConnector, VoterRecord
from states.haryana_dkraj import decode as dkraj_decode

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AC_META_PATH = os.path.join(_HERE, "data", "haryana_ac_meta.json")

BASE = "https://ceoharyana.gov.in"
DISTRICT_URL = BASE + "/WebCMS/GetDistrict2002"
AC_LIST_URL = BASE + "/WebCMS/GetAssemblyConstituencyListByDistrictId2002"
PART_LIST_URL = BASE + "/WebCMS/GetPartNameListByConstitutionId"
PART_PDF_URL = BASE + "/ElectoralRoll2002/CMB{ac}Year2002/CMB0{ac}{part}.pdf"

# ACs whose PDFs are scans rather than text (see module docstring). Recorded
# here as the source of truth used when (re)generating the meta file.
SCANNED_ACS = frozenset({
    1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 28,
    29, 30, 32, 33, 34, 36, 37, 38, 39, 40, 41, 42, 43, 49, 50, 59, 72, 73,
    76, 77, 78, 80, 81, 82, 83, 84,
})

# The relation and gender codes are not guessed at -- they are documented by
# the rolls themselves, in a legend printed in the footer of every data page:
#
#   कॉलम(4): पि -पिता, मा - माता, प - पति, अ - अन्य;
#   कॉलम(6): पु -पुरूष, म - महिला ;
#   कॉलम(7): आयु 1.1.2002 के अनुसार है ;
#   कॉलम(8): निर्वाचक के फोटो पहचान पत्र की क्रम संख्या
#
# which maps exactly onto VoterRecord's F/H/M/O scheme. Independently
# corroborated by ACs 2/31/35, which carry these two columns in real Unicode
# alongside the DK-RAJ text. Only these documented values are normalized;
# anything else keeps its raw value and earns a remark.
RELATION_NORMALIZE = {
    "": "",
    "पि": "F",    # पिता  father
    "िप": "F",    # same, matra visually reordered before its base consonant
                  # by the DK-RAJ font -- confirmed empirically: HR02 part 1
                  # has 0 "पि" cells and 365 "िप" cells, i.e. this reversed
                  # form isn't a rare glitch, it's how this glyph sequence
                  # actually renders.
    "प": "H",     # पति   husband
    "मा": "M",    # माता  mother
    "अ": "O",     # अन्य  other
}

GENDER_NORMALIZE = {
    "": "",
    "पु": "M",    # पुरूष  male
    "म": "F",     # महिला  female
}

# Column indices, as printed in the table's own [1]..[8] header markers.
COL_SERIAL, COL_HOUSE, COL_NAME = 1, 2, 3
COL_RELATION, COL_RELATIVE, COL_GENDER, COL_AGE, COL_EPIC = 4, 5, 6, 7, 8

# A row's cells sit on a common baseline, except the EPIC number which is set
# a few points lower; cluster words into rows with enough slack to keep it.
ROW_TOLERANCE = 5.0
EDGE_TOLERANCE = 3.0


class UnparseableRollError(Exception):
    """Raised for an AC whose PDFs are scans with no usable text layer."""


def _clean(val):
    return re.sub(r"\s+", " ", (val or "")).strip()


def _normalize(raw, table, field_label, remarks):
    """Look up raw in table; on a miss keep the original value as-is and note
    it in remarks rather than guessing at a mapping or dropping it."""
    val = _clean(raw).rstrip(".")
    if val in table:
        return table[val]
    if val:
        remarks.append(f"unrecognized {field_label}: {val!r}")
    return val


def _parse_int(raw, field_label, remarks):
    """Blank is a normal 'not recorded' state (no remark). Anything else that
    fails to parse is a genuine data quirk -- kept as a null field with a
    remark rather than silently dropping the row."""
    val = _clean(raw)
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        remarks.append(f"non-numeric {field_label}: {val!r}")
        return None


def _pad_part(part):
    """The site's own GetFilePath() left-pads the part id to 4 characters as
    a string, so "98A" becomes "098A" rather than being treated as a number."""
    return part.rjust(4, "0")


def _part_candidates(listed):
    """Part ids to attempt for an AC.

    The portal's part list is unreliable in BOTH directions, so it is used as
    a hint rather than as truth:

      * It omits parts that do exist. AC61 lists part "70" twice and never
        lists "71", yet CMB0610071.pdf is a real 289 KB part -- trusting the
        list would silently drop ~1,000 voters.
      * It lists parts that do not exist. AC22 lists "98A", which 404s.

    So we attempt the full contiguous range 1..max alongside whatever the
    portal listed, and let a 404 (recorded in the ZIP manifest) settle which
    parts are real.
    """
    numeric = [int(p) for p in listed if p.isdigit()]
    candidates = {_pad_part(str(i)) for i in range(1, max(numeric, default=0) + 1)}
    candidates.update(_pad_part(p) for p in listed)
    return sorted(candidates)


def _cluster(values, tolerance):
    groups = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _column_bounds(page, words):
    """Recover the 8 column x-ranges from the table's own vertical ruling
    lines, keyed to the printed [1]..[8] header markers. Returns None for a
    page that carries no such table (e.g. the part's cover page)."""
    markers = {}
    for w in words:
        m = re.fullmatch(r"\[([1-8])\]", w["text"])
        if m:
            markers[int(m.group(1))] = (w["x0"] + w["x1"]) / 2
    if len(markers) != 8:
        return None

    rules = _cluster(
        [e["x0"] for e in page.edges if e["orientation"] == "v"], EDGE_TOLERANCE
    )
    bounds = {}
    for col, centre in markers.items():
        left = [x for x in rules if x <= centre]
        right = [x for x in rules if x > centre]
        if not left or not right:
            return None
        bounds[col] = (max(left), min(right))
    return bounds


def _cells(words, bounds):
    """Group words into table rows, then into columns. Words outside every
    column (page furniture in the margins) are dropped."""
    placed = []
    for w in words:
        centre = (w["x0"] + w["x1"]) / 2
        for col, (lo, hi) in bounds.items():
            if lo <= centre < hi:
                placed.append((w, col))
                break

    rows = []
    for w, col in sorted(placed, key=lambda p: ((p[0]["top"] + p[0]["bottom"]) / 2, p[0]["x0"])):
        mid = (w["top"] + w["bottom"]) / 2
        if rows and mid - rows[-1][0] <= ROW_TOLERANCE:
            rows[-1][1].append((w, col))
        else:
            rows.append((mid, [(w, col)]))
    return [cells for _, cells in rows]


def _cell_text(cells, col, remarks):
    """Join a cell's words left-to-right, transcoding the DK-RAJ ones. Words
    in a normal Unicode font (the EPIC numbers are set in Times-Roman) are
    taken literally."""
    parts = []
    for w, c in sorted((wc for wc in cells if wc[1] == col), key=lambda wc: wc[0]["x0"]):
        text = w["text"]
        if "DK-RAJ" in w.get("fontname", ""):
            text, unknown = dkraj_decode(text)
            if unknown:
                codes = ", ".join(sorted({f"0x{ord(u):02x}" for u in unknown}))
                remarks.append(f"undecodable glyph(s) {codes} in column {col}")
        parts.append(text)
    return _clean(" ".join(parts))


class HaryanaConnector(StateConnector):
    state_id = "haryana"

    def __init__(self, request_delay=0.5):
        # One AC means a couple of hundred PDF requests, so this connector
        # keeps a session open and paces itself rather than hammering the
        # portal. The download script wires this to its --rate flag.
        self.request_delay = request_delay
        self.session = requests.Session()

    def list_constituencies(self) -> list:
        with open(AC_META_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return [
            Constituency(
                ac_code=row["ac_code"],
                ac_name=row["ac_name"],
                district=row["district"],
                total_parts=row.get("total_parts", 0),
                extra={"ac_id": row["ac_id"], "roll_format": row["roll_format"]},
            )
            for row in raw
        ]

    # -- fetching ---------------------------------------------------------

    def _post(self, url, data):
        resp = self.session.post(url, data=data, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def list_parts(self, ac_id: int, roll_year: int = 2002) -> list:
        """Part ids for one AC as the portal lists them, de-duplicated and
        with blank entries dropped. Kept as strings: part ids are not always
        numeric (AC22 lists a "98A"), and the site's own URL builder pads
        them as text."""
        rows = self._post(
            PART_LIST_URL, {"ConstitutionId": ac_id, "YearID": roll_year}
        )
        seen, parts = set(), []
        for r in rows:
            p = str(r.get("PartNo", "")).strip()
            if p and p not in seen:
                seen.add(p)
                parts.append(p)
        return parts

    def refresh_meta(self, path=AC_META_PATH) -> list:
        """Walk the district and AC endpoints and (re)write haryana_ac_meta.json.
        Run this to regenerate the static file list_constituencies() reads."""
        meta = []
        for district in self._post(DISTRICT_URL, {"YearID": 2002}):
            for row in self._post(AC_LIST_URL, {"DistrictId": district["ID"]}):
                ac_id = int(row["AC_ID"])
                listed = self.list_parts(ac_id)
                meta.append({
                    "ac_id": ac_id,
                    "ac_code": f"HR{ac_id:02d}",
                    "ac_name": _clean(row["ACName"]),
                    "district": _clean(district["DistrictName"]),
                    # How many part PDFs fetch_raw() will attempt. This can
                    # exceed what the portal lists -- see _part_candidates().
                    "total_parts": len(_part_candidates(listed)),
                    "listed_parts": len(listed),
                    "roll_format": "scanned" if ac_id in SCANNED_ACS else "text",
                })
        meta.sort(key=lambda r: r["ac_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return meta

    def fetch_raw(self, ac: Constituency, roll_year: int) -> bytes:
        """Download every part PDF for this AC, bundled into one ZIP."""
        if roll_year != 2002:
            raise NotImplementedError(
                "Haryana connector only implements the 2002 Intensive Revision "
                "roll; later rolls are published through a different portal."
            )
        ac_id = ac.extra["ac_id"]
        listed = self.list_parts(ac_id, roll_year)
        candidates = _part_candidates(listed)

        fetched, missing = [], []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, part in enumerate(candidates):
                if i:
                    time.sleep(self.request_delay)
                url = PART_PDF_URL.format(ac=f"{ac_id:02d}", part=part)
                resp = self.session.get(url, timeout=120)
                if resp.status_code == 404:
                    missing.append(part)
                    continue
                resp.raise_for_status()
                zf.writestr(f"part{part}.pdf", resp.content)
                fetched.append(part)
            zf.writestr("manifest.json", json.dumps({
                "ac_code": ac.ac_code,
                "ac_id": ac_id,
                "roll_year": roll_year,
                "listed_by_portal": listed,
                "fetched": fetched,
                "missing": missing,
            }, indent=1))
        if not fetched:
            raise RuntimeError(f"{ac.ac_code}: no part PDFs could be fetched")
        return buf.getvalue()

    # -- parsing ----------------------------------------------------------

    def parse_raw(self, raw: bytes, ac: Constituency, roll_year: int) -> list:
        if ac.extra.get("roll_format") == "scanned":
            raise UnparseableRollError(
                f"{ac.ac_code} ({ac.ac_name}) is published as page scans with no "
                "usable text layer; it needs an OCR pipeline, which this "
                "connector does not implement. See states/haryana.py."
            )
        records = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in sorted(zf.namelist()):
                m = re.fullmatch(r"part(\w+)\.pdf", name)
                if not m:
                    continue  # manifest.json
                records.extend(
                    self._parse_part(zf.read(name), m.group(1), ac, roll_year)
                )
        return records

    def _parse_part(self, pdf_bytes, part_id, ac, roll_year):
        # VoterRecord.part_no is an int, but a handful of part ids carry a
        # letter suffix. Keep the numeric prefix and flag the row rather than
        # dropping the part or silently conflating "98A" with "98".
        digits = re.match(r"0*(\d+)", part_id)
        part_no = int(digits.group(1)) if digits else None
        suffix_remark = (
            "" if re.fullmatch(r"0*\d+", part_id)
            else f"source part id is {part_id.lstrip('0')!r}, recorded as part {part_no}"
        )
        return self._parse_pages(pdf_bytes, part_no, suffix_remark, ac, roll_year)

    def _parse_pages(self, pdf_bytes, part_no, suffix_remark, ac, roll_year):
        records = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(extra_attrs=["fontname"])
                bounds = _column_bounds(page, words)
                if bounds is None:
                    continue  # cover/summary page, carries no voter table
                for cells in _cells(words, bounds):
                    rec = self._parse_row(cells, part_no, suffix_remark, ac, roll_year)
                    if rec is not None:
                        records.append(rec)
        return records

    def _parse_row(self, cells, part_no, suffix_remark, ac, roll_year):
        """Turn one table row into a VoterRecord, or return None for the rows
        that are not voter entries (the header row, and the section headings
        -- अनुभाग संख्या -- that break a part into localities)."""
        remarks = [suffix_remark] if suffix_remark else []
        serial_raw = _cell_text(cells, COL_SERIAL, remarks)
        if not re.fullmatch(r"\d+", serial_raw):
            return None

        name = _cell_text(cells, COL_NAME, remarks)
        relative = _cell_text(cells, COL_RELATIVE, remarks)
        house = _cell_text(cells, COL_HOUSE, remarks)
        # Parsed and checked, but VoterRecord has nowhere to put it yet.
        _epic = _cell_text(cells, COL_EPIC, remarks)

        if not name:
            remarks.append("no voter name in row")

        return VoterRecord(
            state=self.state_id,
            district=ac.district,
            ac_code=ac.ac_code,
            ac_name=ac.ac_name,
            part_no=part_no,
            serial_no=_parse_int(serial_raw, "serial_no", remarks),
            local_ref=house,
            full_name=name,
            full_relative_name=relative,
            relation_code=_normalize(
                _cell_text(cells, COL_RELATION, remarks),
                RELATION_NORMALIZE, "relation_code", remarks,
            ),
            age=_parse_int(_cell_text(cells, COL_AGE, remarks), "age", remarks),
            gender=_normalize(
                _cell_text(cells, COL_GENDER, remarks),
                GENDER_NORMALIZE, "gender", remarks,
            ),
            roll_year=roll_year,
            remark="; ".join(remarks),
        )
