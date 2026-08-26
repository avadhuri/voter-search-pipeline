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
