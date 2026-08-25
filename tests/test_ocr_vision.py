"""
The resume check, driven by the way it actually failed.

`annotate` is resumable because a window file on disk means those pages are
done. That is true of every failure this script was written to survive -- a
kill, a 429, a dropped connection -- and false of exactly one: a *page-level*
error, which Vision returns inside a 200 OK. Those windows were written like
any other, so the next run skipped them, so their electors were absent from
the built AC with a clean log. 31 pages across AC287 and AC291 are on disk
that way, holding "The service is currently unavailable." and nothing else.

Both halves are tested here: not creating new ones (the retry), and not
skipping the ones already made (`--retry-failed`).
"""
import gzip
import json

import pytest

import ocr_vision


def _write(part_dir, first, last, responses):
    part_dir.mkdir(parents=True, exist_ok=True)
    path = part_dir / f"p{first:04d}-{last:04d}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"responses": responses}, fh)
    return path


def _err(message):
    return {"error": {"code": 3, "message": message}}


def _ok():
    return {"fullTextAnnotation": {"pages": []}}


def test_a_window_already_on_disk_is_not_re_billed(tmp_path):
    _write(tmp_path, 1, 5, [_ok()] * 5)
    assert not ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=False)
    assert not ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=True)


def test_a_window_never_written_is_pending_either_way(tmp_path):
    assert ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=False)
    assert ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=True)


def test_a_transient_failure_on_disk_is_pending_only_under_retry_failed(tmp_path):
    _write(tmp_path, 1, 5, [_ok(), _err("The service is currently unavailable."),
                            _ok(), _ok(), _ok()])
    # Default stays the cheap check: a plain resume must never re-bill.
    assert not ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=False)
    assert ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=True)


def test_a_permanent_failure_is_not_retried_however_often_it_is_asked(tmp_path):
    # AC287 part0103's whole PDF. Re-submitting it forever costs money and
    # changes nothing; naming it is states/west_bengal.py's job.
    _write(tmp_path, 1, 5, [_err("Bad image data.")] * 5)
    assert not ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=True)


def test_an_unreadable_window_file_is_pending_rather_than_trusted(tmp_path):
    path = tmp_path / "p0001-0005.json.gz"
    path.write_bytes(b"not gzip")
    assert ocr_vision._window_pending(tmp_path, [1, 2, 3, 4, 5], retry_failed=True)


def test_a_transient_page_error_is_retried_before_the_window_is_written(monkeypatch):
    """The fix at the point the file gets created, not just at resume time."""
    calls = []
    replies = [
        {"responses": [{"responses": [_ok(), _err("The service is currently unavailable.")]}]},
        {"responses": [{"responses": [_ok(), _ok()]}]},
    ]

    def fake_api(method, url, project, body=None):
        calls.append(body["requests"][0]["pages"])
        return replies[len(calls) - 1]

    monkeypatch.setattr(ocr_vision, "_api", fake_api)
    monkeypatch.setattr(ocr_vision.time, "sleep", lambda *_: None)
    out = ocr_vision._annotate_window("gs://b/x.pdf", [1, 2], "proj")
    assert len(calls) == 2, "the transient page must have been re-submitted"
    assert all("error" not in r for r in out)


def test_a_permanent_page_error_is_written_through_rather_than_looping(monkeypatch):
    calls = []

    def fake_api(method, url, project, body=None):
        calls.append(body["requests"][0]["pages"])
        return {"responses": [{"responses": [_err("Bad image data.")] * 2}]}

    monkeypatch.setattr(ocr_vision, "_api", fake_api)
    monkeypatch.setattr(ocr_vision.time, "sleep", lambda *_: None)
    out = ocr_vision._annotate_window("gs://b/x.pdf", [1, 2], "proj")
    assert len(calls) == 1
    assert [r["error"]["message"] for r in out] == ["Bad image data."] * 2
