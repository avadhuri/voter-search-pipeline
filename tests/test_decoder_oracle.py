"""Self-tests for the decoder oracle's two counting rules.

Both rules exist because getting them wrong produced a wrong answer we acted
on, and neither failure was visible in the report it printed:

  breadth   Counted over the characters the diff could BLAME, a glyph that
            contributes no character at its own position can never be blamed,
            so it recorded zero constituencies and was filed under "confined
            to one or two ACs". Gid 206 -- a pre-base matra, drawn to the left
            of the cluster it belongs to -- came back 128/128 cells
            disagreeing and was dismissed on that basis. It was wrong in
            every constituency it appeared in.

  dedup     Counted per ROW, one part's dominant surname carries a whole
            glyph id. AC039/part0050 is mostly কর্ম্মকার, which we decode
            correctly and Vision misreads identically every time; per-row
            counting turned that single observation into gid 151 at z = 14.1.

They are asserted here rather than left to a comment because the oracle is
the instrument the glyph table is judged by, and an instrument with a
miscalibrated scale reports confidently either way.
"""

import decoder_oracle as do
from states import west_bengal as _patches_pdfminer  # noqa: F401  (side effect)
from states import west_bengal_shreelipi as sl

GID_KA = 36          # ক
GID_AI_MATRA = 206   # ৈ, drawn BEFORE the consonant it follows in reading order
GID_DA = 101         # দ, a control: it appears only in the cells that agree


def pua(*gids):
    """The undecoded glyph-id string the extractor hands the oracle."""
    return "".join(chr(sl.PUA_BASE + g) for g in gids)


def test_a_prebase_matra_contributes_no_character_to_blame():
    """The premise the breadth rule rests on -- asserted, not assumed.

    If this ever stops being true the next test stops testing anything, so
    it fails here first and says why.
    """
    cell = pua(GID_AI_MATRA, GID_KA)
    assert sl.decode(cell)[0] == "কৈ"
    hit, _chars = do.implicated(cell, "কৈ", "কে")
    assert GID_KA in hit
    assert GID_AI_MATRA not in hit, (
        "a pre-base matra was blamable after all -- rewrite the breadth test"
    )
    assert GID_AI_MATRA in do.gids_of(cell)


def test_breadth_counts_a_glyph_the_diff_cannot_blame():
    """Gid 206's miss, asserted directly.

    Three constituencies disagree on a cell containing the matra. Breadth
    counted over `implicated` records nothing for it and the id is discarded
    for narrowness; breadth counted over every glyph PRESENT records all
    three, which is what it is.
    """
    tally = do.Tally()
    cell = pua(GID_AI_MATRA, GID_KA)
    for ac in ("AC001", "AC050", "AC200"):
        tally.add(cell, "কৈ", "কে", False, f"{ac}/part0001.pdf")
    tally.finalize()

    assert tally.disagree == 3
    assert tally.acs_disagreeing[GID_AI_MATRA] == {"AC001", "AC050", "AC200"}
    assert len(tally.acs_disagreeing[GID_AI_MATRA]) >= do.MIN_ACS
    assert tally.char_blame[GID_AI_MATRA] == 0, (
        "the matra is still unblamable -- breadth must not come from blame"
    )
    assert tally.examples[GID_AI_MATRA], "a flagged id must carry examples to read"


def _repeated_surname_cells():
    """One AC, one part, the same name 200 times, all misread the same way."""
    return pua(GID_KA, GID_KA, GID_KA), "কককখ", "কককগ"


def test_a_repeated_surname_counts_once_and_flags_nothing():
    tally = do.Tally()
    cell, ours, theirs = _repeated_surname_cells()
    for _ in range(200):
        tally.add(cell, ours, theirs, False, "AC039/part0050.pdf")

    # 400 ordinary cells that agree, so there is a base rate to stand
    # against. Deliberately built from a DIFFERENT consonant: sharing one
    # with the surname would spread its glyph across the whole population
    # and there would be no concentration left to measure either way.
    for i in range(400):
        # Each cell is a distinct name, or the evidence unit under test
        # would collapse the base population too and there would be nothing
        # to measure the surname against.
        tally.add(pua(GID_DA, GID_DA) + str(i), f"দদ{i}", f"দদ{i}", False,
                  f"AC{i % 40:03d}/part{i:04d}.pdf")

    assert tally.skipped_duplicate == 199, "the 200 rows are one observation"
    report = do.build_report(tally, {"seed": 0})
    row = next(r for r in report["gids"] if r["gid"] == GID_KA)
    assert row["cells"] == 1
    assert row["gid"] not in [f["gid"] for f in report["flagged"]]


def test_the_same_data_counted_per_row_would_have_manufactured_a_z_score():
    """The teeth: without dedup this fixture is an extreme, confident finding.

    Constructed by filling `records` directly with a distinct key per row --
    the pre-dedup evidence unit -- rather than by calling `add`, so the only
    thing that differs between this test and the one above is the counting
    rule under examination.
    """
    tally = do.Tally()
    cell, ours, theirs = _repeated_surname_cells()
    for i in range(200):
        tally.records[(f"AC039#{i}", cell)] = (ours, theirs,
                                               "AC039/part0050.pdf")
    for i in range(400):
        tally.records[(f"AC{i % 40:03d}#{i}", pua(GID_DA, GID_DA))] = (
            "দদ", "দদ", f"AC{i % 40:03d}/part0001.pdf")

    report = do.build_report(tally, {"seed": 0})
    row = next(r for r in report["gids"] if r["gid"] == GID_KA)
    assert row["cells"] == 200
    assert row["z"] > 14.0, f"expected a manufactured z-score, got {row['z']:.1f}"
    assert row["z"] >= do.Z_FLAG and row["lift"] >= do.LIFT_FLAG


# --------------------------------------------------- what the verdict claims

GID_DHA = 222        # the real id from #36: 11 cells, 11 ACs, wrong in all 11
GID_BROAD = 76       # ট, the real id from #37 that does clear the bar


SAMPLE = {"seed": 0, "parts": 1, "pages": 1, "acs": 1, "districts": 1,
          "skipped_font": 0, "dpi": 300, "engine": "fixture"}


def _baseline(tally, n=400):
    """Cells that agree, so the report has a corpus rate to stand against."""
    for i in range(n):
        tally.add(pua(GID_KA, GID_KA) + str(i), f"কক{i}", f"কক{i}", False,
                  f"AC{i % 40:03d}/part{i:04d}.pdf")


def _wrong_in_every_cell(tally, gid, n_acs, first_ac=100):
    """One id, wrong in 100% of its cells, one cell per constituency.

    Shaped on the real gid 222: 11 distinct names across 11 ACs, every one
    of them losing the same ধ out of a conjunct.

    The carrier glyph is the same one `_baseline` uses, deliberately. A
    filler of its own would ride along in every disagreeing cell and clear
    the bar itself -- breadth is counted over every gid PRESENT in the cell,
    not only the blamed one -- and the fixture would then be testing an
    artefact of its own construction.
    """
    for i in range(n_acs):
        ac = f"AC{first_ac + i:03d}"
        tally.add(pua(GID_KA, gid) + str(i), f"কধ{i}", f"কব{i}", False,
                  f"{ac}/part0001.pdf")


def test_an_id_wrong_in_every_cell_it_appears_in_clears_no_bar():
    """The finding the verdict wording has to stop hiding.

    Nothing here is marginal: the id is wrong every single time, in eleven
    separate constituencies, at a lift of ~37 over the corpus. It clears
    neither `flagged` nor `narrow`, because both require cells >= MIN_CELLS
    and it has eleven. `narrow` is the report's own "ruled out" list, so the
    id is not merely unflagged: there is no line in either verdict that
    accounts for it. It can still surface as a row in the ranked residual
    table, which is where #37's nine were found -- by reading the table, not
    by the verdict saying anything.
    """
    tally = do.Tally()
    _baseline(tally)
    _wrong_in_every_cell(tally, GID_DHA, 11)

    report = do.build_report(tally, {"seed": 0})
    row = next(r for r in report["gids"] if r["gid"] == GID_DHA)
    assert row["cells"] == 11 and row["disagreeing"] == 11
    assert row["rate"] == 1.0
    assert row["acs"] == 11, "wrong in eleven constituencies, not one"
    assert row["z"] >= do.Z_FLAG and row["lift"] >= do.LIFT_FLAG

    assert row["cells"] < do.MIN_CELLS
    assert GID_DHA not in [r["gid"] for r in report["flagged"]]
    assert GID_DHA not in [r["gid"] for r in report["narrow"]]


def test_a_verdict_of_none_does_not_claim_the_table_is_clean(capsys):
    """Same tally as above: nine real defects of this shape, verdict silent.

    The old wording was "CONVERGED, on this evidence", printed while an id
    wrong in 100% of its cells sat in the corpus. What the run actually
    established is narrower and is now what it says.
    """
    tally = do.Tally()
    _baseline(tally)
    _wrong_in_every_cell(tally, GID_DHA, 11)
    do.print_report(do.build_report(tally, SAMPLE))
    out = capsys.readouterr().out

    assert not report_claims_convergence(out)
    assert "no glyph id is above the concentration threshold" in out
    assert f"cells>={do.MIN_CELLS}" in out
    assert "not defects" in out


def test_a_verdict_of_two_is_not_a_claim_that_there_are_two_defects(capsys):
    """The line ruling 1 was about, with the id that does clear the bar."""
    tally = do.Tally()
    _baseline(tally)
    _wrong_in_every_cell(tally, GID_BROAD, 60, first_ac=200)
    _wrong_in_every_cell(tally, GID_DHA, 11)

    report = do.build_report(tally, SAMPLE)
    assert [r["gid"] for r in report["flagged"]] == [GID_BROAD]

    do.print_report(report)
    out = capsys.readouterr().out
    assert "1 glyph id(s) above the concentration threshold" in out
    assert "own a concentration of the residual" not in out
    assert not report_claims_convergence(out)
    assert "not defects" in out


def report_claims_convergence(out):
    """The word carried the overstatement, so no verdict may still use it."""
    return "CONVERGED" in out
