"""What the figures script promises about itself.

Its whole reason to exist is that a number written as fact goes on reading
as fact, so the things worth locking are the two ways it could quietly
restate what its own numbers mean: summing overlapping classes, and deciding
what counts as a cell that was read.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import wb_ocr_figures as figures        # noqa: E402


def _row(**over):
    base = {"serial_no": 1, "serial_wide": None, "full_name": "ক খ",
            "relation_code": "F", "full_relative_name": "গ ঘ", "gender": "M",
            "age": 30, "local_ref": "WB/42/287/000001", "remark": ""}
    base.update(over)
    return base


class _Connector:
    """Stands in for the real module: parse_part is the only surface used."""

    def __init__(self, by_part):
        self.by_part = by_part

    def parse_part(self, part_dir):
        return self.by_part.get(Path(part_dir).name, [])


def _corpus(tmp_path, parts):
    ocr = tmp_path / "ocr" / "AC287"
    for name in parts:
        (ocr / name).mkdir(parents=True)
    return str(tmp_path / "ocr")


def test_the_recovered_classes_are_counted_as_a_union_not_a_sum(tmp_path):
    """One real row in the corpus is both glued and `অন্য`, so summing the
    class columns reports 728 rows where 727 exist. Off by one is the size
    at which this is worth locking, not the size at which it matters: the
    same summation over a future class could be off by thousands.
    """
    both = _row(relation_code="O",
                remark="relation word read glued to the next token 'মহঃ'")
    rows = [both, _row(relation_code="O"),
            _row(remark="relation word read glued to the next token 'নবেন'")]
    ocr = _corpus(tmp_path, ["part0001"])
    total, _, _ = figures.measure(_Connector({"part0001": rows}), ocr, ["AC287"], 5)

    assert total["recovered"] == 3
    classes = sum(total[label] for label, _ in figures.RECOVERED)
    assert classes == 4, "the classes are expected to overlap; that is the point"


def test_a_serial_read_as_zero_counts_as_read_and_is_reported(tmp_path):
    """A serial of 0 is a cell that was read badly, not a cell that was not
    read. Counting it as unread would move the recovery figure this script
    exists to keep honest, so the rule is presence and the zeros are printed
    separately rather than folded into either side.
    """
    rows = [_row(serial_no=0), _row(serial_no=7), _row(serial_no=None)]
    ocr = _corpus(tmp_path, ["part0001"])
    total, _, _ = figures.measure(_Connector({"part0001": rows}), ocr, ["AC287"], 5)

    assert total["serial"] == 2
    assert total["serial zero"] == 1


def test_an_empty_string_is_not_a_cell_that_was_read(tmp_path):
    """The other half of the same rule: the string fields use "" for absent,
    so presence cannot simply mean `is not None`."""
    rows = [_row(gender="", local_ref=""), _row()]
    ocr = _corpus(tmp_path, ["part0001"])
    total, _, _ = figures.measure(_Connector({"part0001": rows}), ocr, ["AC287"], 5)

    assert total["sex"] == 1
    assert total["EPIC"] == 1


def test_the_worst_parts_are_ranked_by_rate_not_by_count(tmp_path):
    """A big part with more blanks is not a worse part. The docstring quotes
    a rate and a part name together, so the ranking has to be the rate."""
    small = [_row(full_name="")] + [_row() for _ in range(9)]        # 10%
    large = [_row(full_name="") for _ in range(5)] + [_row() for _ in range(495)]
    ocr = _corpus(tmp_path, ["part0001", "part0002"])
    _, _, worst = figures.measure(
        _Connector({"part0001": small, "part0002": large}), ocr, ["AC287"], 5)

    assert [name for _, name, _, _ in worst] == ["AC287/part0001", "AC287/part0002"]


def test_a_checkout_with_no_ocr_output_says_so_and_leaves(tmp_path, capsys):
    """Gigabytes of paid API output is not something every checkout has, and
    a make target that fails there would be a target nobody runs."""
    assert figures.main(["--raw-dir", str(tmp_path / "nothing")]) == 0
    assert "nothing to measure" in capsys.readouterr().err


def test_an_ac_with_no_ocr_output_is_an_error_not_an_empty_report(tmp_path):
    """Asking for AC291 and getting a clean report over nothing is how a
    figure gets quoted for an AC that was never measured."""
    _corpus(tmp_path, ["part0001"])
    with pytest.raises(SystemExit):
        figures.main(["--raw-dir", str(tmp_path), "--acs", "AC291"])
