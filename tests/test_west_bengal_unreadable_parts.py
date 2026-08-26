"""An AC with a part this reader cannot open is absent, not short, and not
accused of being damaged.

Three separate things, and each one has already been got wrong once:

* **Absent, not fatal.** pdfminer raising out of `pdfplumber.open` on
  AC088 part0065 propagated through `_build_one_ac` -- which catches
  `UnparseableRollError` and, by an explicit decision recorded there,
  nothing else -- out through the parent pool's `fut.result()`, and took
  down a 294-AC run of every state at once.
* **Absent, not short.** 231 of AC088's 233 parts open. Building those is
  the option that costs nothing visible and loses exactly the electors
  nobody would notice missing, which is why the AC is refused whole,
  following `_parse_scanned`'s gap check.
* **Not accused.** This refusal was first specified as "these PDFs are
  corrupt". They are not: pdfium opens part0065 at 26 pages and part0068
  at 23, and all three text layers -- the control part beside them
  included -- begin with the byte-identical glyph-id sequence. Same roll,
  same font, a reader that cannot open them. The message a build log
  compiles into is the artifact a maintainer trusts most, so the wording
  is asserted here rather than left to whoever edits it next.
"""
import io
import os
import zipfile

import pytest

from states import west_bengal as wb
from states.base import Constituency, UnparseableRollError
from states.west_bengal import WestBengalConnector, _second_reader_says


AC = Constituency(ac_code="AC088", ac_name="ASHOKENAGAR",
                  district="North 24 Parganas")

GARBAGE = b"not a pdf at all"


def _zero_page_pdf():
    """A PDF pdfplumber opens and then reports as holding no pages at all.

    Hand-built rather than fetched: it is the shape of AC088 part0068, which
    fails this way rather than by raising, and a test for that half of the
    guard should not need a 62 MB download to run.
    """
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [] /Count 0 >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref))
    return out.getvalue()


def _one_page_pdf():
    """The smallest PDF both readers agree holds exactly one page."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, xref))
    return out.getvalue()


def _zip_of(blobs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, blob in blobs.items():
            zf.writestr(name, blob)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def _parse(blobs, monkeypatch=None, good=None):
    """Run _parse_typeset over `blobs`, standing in for the parts that open.

    `good` names the members whose parse should succeed; they are given rows
    by a stub rather than real PDFs, because what is under test here is what
    the loop does with the parts that fail, and a real readable part costs a
    download the rest of this module does not need.
    """
    zf = _zip_of(blobs)
    if good:
        real = WestBengalConnector._parse_part

        def stub(self, pdf_bytes, ac, roll_year, part_no, member, dropped=None):
            if member in good:
                return [f"{member}-row"]
            return real(self, pdf_bytes, ac, roll_year, part_no, member, dropped)

        monkeypatch.setattr(WestBengalConnector, "_parse_part", stub)
    return WestBengalConnector()._parse_typeset(
        zf, sorted(blobs), AC, 2002)


# --------------------------------------------------------------------------
# the guard itself

def test_a_part_that_raises_out_of_the_reader_is_named_in_the_refusal():
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE})
    said = str(exc.value)
    assert "part0001.pdf" in said
    assert "PdfminerException" in said


def test_a_part_that_opens_with_no_pages_is_named_too():
    """The other half: pdfplumber opens it and hands back an empty page list.

    Reported differently from a raise on purpose -- it is the same failure to
    read, but a maintainer chasing it needs to know which one they are looking
    at before they open the file.
    """
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": _zero_page_pdf()})
    said = str(exc.value)
    assert "part0001.pdf" in said
    assert "0 pages" in said


def test_every_unreadable_part_is_named_not_just_the_one_that_stopped_it():
    """The loop carries on past a failure. A refusal naming only the first of
    two sends a repair back for a second round it did not need."""
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE,
                "part0002.pdf": _zero_page_pdf()})
    said = str(exc.value)
    assert "part0001.pdf" in said and "part0002.pdf" in said
    assert "could not open 2 of 2 part(s)" in said


def test_the_ac_is_refused_whole_rather_than_built_from_the_parts_that_open(
    monkeypatch,
):
    """The expensive half of the decision, asserted: two readable parts are
    thrown away rather than published as the AC."""
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": b"", "part0002.pdf": GARBAGE,
                "part0003.pdf": b""},
               monkeypatch, good={"part0001.pdf", "part0003.pdf"})
    assert "2 part(s) that do open" in str(exc.value)


def test_an_ac_whose_parts_all_open_is_unaffected(monkeypatch):
    records = _parse({"part0001.pdf": b"", "part0002.pdf": b""},
                     monkeypatch, good={"part0001.pdf", "part0002.pdf"})
    assert records == ["part0001.pdf-row", "part0002.pdf-row"]


def test_a_reader_error_this_code_has_not_seen_before_is_still_carried(
    monkeypatch,
):
    """The catch is broad deliberately. A new pdfminer version raising a type
    nobody here has heard of must refuse the AC with the message attached, not
    go back to killing the run -- which is the whole defect, one release
    later."""
    class SomethingNew(Exception):
        pass

    def boom(self, *a, **kw):
        raise SomethingNew("a shape this code has never seen")

    monkeypatch.setattr(WestBengalConnector, "_parse_part", boom)
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE})
    said = str(exc.value)
    assert "SomethingNew" in said and "never seen" in said


# --------------------------------------------------------------------------
# what the refusal says, which is the part that ends up in a build log

def test_the_refusal_blames_the_reader_and_not_the_source():
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE})
    said = str(exc.value).lower()
    assert "this connector's pdf reader (pdfminer, via pdfplumber)" in said
    assert "not a verdict on the source" in said
    for accusation in ("corrupt", "damaged", "broken pdf", "bad pdf",
                       "invalid pdf", "malformed"):
        assert accusation not in said, f"the message claims the source is {accusation}"


def test_the_refusal_points_at_a_live_ticket():
    """A message that says only "we cannot read this" leaves the 231 readable
    parts as nobody's work. It cites the issue that owns recovering them."""
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE})
    assert wb.RECOVERY_ISSUE in str(exc.value)
    assert "issues/52" in wb.RECOVERY_ISSUE


def test_the_refusal_says_what_a_second_reader_made_of_the_same_bytes():
    with pytest.raises(UnparseableRollError) as exc:
        _parse({"part0001.pdf": GARBAGE})
    assert "pdfium" in str(exc.value)


# --------------------------------------------------------------------------
# the cross-check, whose three answers must stay three answers

def test_the_cross_check_reports_a_page_count_when_it_can_read_the_bytes():
    """Against a PDF built here, so the readable answer is asserted on every
    machine and not only where the 62 MB AC088 download happens to be."""
    pytest.importorskip("pypdfium2")
    assert _second_reader_says(_one_page_pdf()) == "pdfium reads it at 1 page(s)"


def test_the_cross_check_says_it_could_not_read_them_either():
    pytest.importorskip("pypdfium2")
    assert _second_reader_says(GARBAGE) == "pdfium could not open it either"


def test_the_cross_check_says_it_did_not_run_rather_than_reporting_a_zero(
    monkeypatch,
):
    """"pypdfium2 is not installed here" and "pdfium could not open it" are
    different facts, and a refusal that says the second when it means the
    first re-asserts the damage claim this message exists to avoid."""
    import builtins

    real_import = builtins.__import__

    def no_pypdfium2(name, *a, **kw):
        if name == "pypdfium2":
            raise ImportError("no module named pypdfium2")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pypdfium2)
    said = _second_reader_says(GARBAGE)
    assert said == "not cross-checked, pypdfium2 is not installed here"
    assert "could not" not in said


# --------------------------------------------------------------------------
# against the parts it was found on

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw",
                       "west_bengal")
_REAL_ZIP = os.path.join(RAW_DIR, "AC088.zip")


@pytest.mark.skipif(not os.path.exists(_REAL_ZIP), reason="raw data not downloaded")
def test_ac088_parts_65_and_68_are_the_two_failures_and_pdfium_reads_both():
    """The real bytes, both halves of the guard and the cross-check at once.

    _parse_typeset is not called: parsing 231 readable parts to reach the two
    that fail costs minutes and proves nothing this does not. The two failures
    are driven straight through _parse_part, and the control part beside them
    is parsed too, so a run where *every* part fails cannot pass this test.
    """
    with zipfile.ZipFile(_REAL_ZIP) as zf:
        blobs = {m: zf.read(m) for m in
                 ("part0064.pdf", "part0065.pdf", "part0068.pdf")}

    conn = WestBengalConnector()
    assert conn._parse_part(blobs["part0064.pdf"], AC, 2002, 64,
                            "part0064.pdf"), "the control part still reads"

    with pytest.raises(Exception) as raised:
        conn._parse_part(blobs["part0065.pdf"], AC, 2002, 65, "part0065.pdf")
    assert type(raised.value).__name__ == "PdfminerException"

    with pytest.raises(wb._PartHasNoPages):
        conn._parse_part(blobs["part0068.pdf"], AC, 2002, 68, "part0068.pdf")

    pytest.importorskip("pypdfium2")
    assert _second_reader_says(blobs["part0065.pdf"]) == "pdfium reads it at 26 page(s)"
    assert _second_reader_says(blobs["part0068.pdf"]) == "pdfium reads it at 23 page(s)"
