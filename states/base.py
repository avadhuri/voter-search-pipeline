"""
State connector interface.

Adding a new state to this project means writing one module against this
interface (plus a reconnaissance pass to find that state's data source) --
the DB schema, matching, search, and UI code are all state-agnostic and
don't need to change.
"""
from dataclasses import dataclass, field


class UnparseableRollError(Exception):
    """Raised by parse_raw() for an AC this connector cannot parse at all.

    This is a *declared outcome*, not a failure. Some states publish a
    minority of their ACs as page scans with no text layer; a connector
    that refuses to guess at those is behaving correctly, and the build
    treats them as absent rather than aborting.

    That distinction is the whole point of having a named exception here
    instead of a state-private one. `build_db` catches this and only this:
    every other exception out of parse_raw() is an unexpected fault and
    still stops the build loudly, per the house rule. Before this existed,
    one scanned Haryana AC (HR18 Samalkha) killed a two-state per-AC build
    after 44 ACs had already been written -- taking West Bengal, which had
    not started, and every catalog, which is written last, down with it.

    A connector raising this must name the AC and say why, because the
    build's summary is the only place a reader learns the AC is missing.
    """


@dataclass
class Constituency:
    """One assembly constituency (AC), as listed by a state's electoral portal."""
    ac_code: str          # e.g. "A085" -- state-specific format, treated as opaque
    ac_name: str
    district: str
    total_parts: int = 0
    extra: dict = field(default_factory=dict)  # state-specific metadata, if any


@dataclass
class VoterRecord:
    """
    One voter roll entry, normalized to a common shape across states.

    India's electoral-roll conventions (district/AC/part/serial numbering,
    name + relative-name + relation-code, age, gender) are shared enough
    across states that this shape shouldn't need to change per state --
    state differences belong in each connector's parse_raw(), not here.
    """
    state: str             # e.g. "karnataka"
    district: str
    ac_code: str
    ac_name: str
    part_no: int
    serial_no: int
    local_ref: str
    full_name: str
    full_relative_name: str
    relation_code: str     # F/H/M/O -- Father/Husband/Mother/Other
    age: int
    gender: str
    roll_year: int          # e.g. 2002 or 2025
    remark: str = ""        # human-readable note on any source-data quirk in this row
    locality: str = ""      # village/town/area name, where the source carries one

    # Romanized forms of the two name fields, for a state whose rolls aren't
    # in Latin script. Optional, and only worth setting if the connector's
    # own extraction produced them -- a source that publishes both scripts,
    # or an OCR/decode step that emits a Latin reading alongside the native
    # one. Leave them empty otherwise: build_db.py runs
    # transliteration.backfill_latin_columns() over every non-Latin state,
    # which fills exactly the rows left blank here and never overwrites a
    # value a connector set.
    #
    # A connector-supplied value wins deliberately, not by accident of
    # ordering. The backfill is rule-based ITRANS, and its output is a
    # transliteration scheme rather than a name -- readable enough for
    # fuzzy matching in Devanagari or Telugu, actively wrong in Tamil
    # (ச -> "jh", so Selvam becomes "jhelvam") and incomplete in Malayalam
    # (chillu forms pass through untransliterated). A connector that saw
    # the source knows better in every case. See transliteration.py's
    # module docstring for the measured per-script numbers.
    full_name_latin: str = ""
    full_relative_name_latin: str = ""


class StateConnector:
    """
    Base class for a state's electoral-roll data source. Subclass this and
    implement the three methods below; nothing else in the pipeline needs
    to know how a given state's portal works.
    """

    state_id: str = None    # short slug, e.g. "karnataka" -- set by subclass

    def list_constituencies(self) -> list[Constituency]:
        """Return every AC this connector knows how to fetch."""
        raise NotImplementedError

    def fetch_raw(self, ac: Constituency, roll_year: int) -> bytes:
        """Fetch the raw source bytes (CSV/PDF/etc.) for one AC's roll."""
        raise NotImplementedError

    def parse_raw(self, raw: bytes, ac: Constituency, roll_year: int) -> list[VoterRecord]:
        """Parse fetch_raw()'s output into normalized VoterRecords."""
        raise NotImplementedError
