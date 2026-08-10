"""
State connector interface.

Adding a new state to this project means writing one module against this
interface (plus a reconnaissance pass to find that state's data source) --
the DB schema, matching, search, and UI code are all state-agnostic and
don't need to change.
"""
from dataclasses import dataclass, field


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
