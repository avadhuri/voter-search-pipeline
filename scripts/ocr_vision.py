"""OCR the page-scan constituencies through Google Cloud Vision.

A handful of West Bengal ACs (AC287, AC291, AC294) ship as page scans with
no text layer at all, so `states/west_bengal.py` refuses them -- there are no
glyphs to decode, only pixels. This script is the missing front half for
those: it runs the scans through Vision's DOCUMENT_TEXT_DETECTION and lands
the raw responses beside the zips, where a connector can read them as if they
were a text layer.

Vision is used rather than a local engine because of a measured bake-off
(Vision / Tesseract / Surya on 278 ground-truth name cells decoded from the
digital rolls' own font table). Two findings decided it: Vision holds its
accuracy at 200 dpi where Tesseract falls off (83.2% vs 75.2% exact), and two
of these three ACs are 200 dpi; and on the real scans Tesseract found 58 valid
EPIC numbers against Vision's 107. Surya matched Vision on accuracy but needs
a VLM inference server, measured at 48.9s/page on an M1 Pro even fully batched
onto the GPU -- about a week for this corpus.

**Which endpoint, and why it is not the asynchronous one.** Vision offers
`files:asyncBatchAnnotate` (GCS in, GCS out, long-running) and `files:annotate`
(synchronous, at most five pages per request). They bill identically, per page,
and the obvious choice for 11k pages is the batch one. It is the wrong one:
holding the PDF bytes, the page box and the row grouper all fixed, and varying
only the endpoint, the sex column came back on **67.2%** of rows through
`asyncBatchAnnotate` and **96.4%** through `files:annotate` -- 29 points, on
the same three pages of the same object in the same bucket. The two rasterize
a PDF differently and the async path renders these scans materially worse.
`files:annotate` reading from `gcsSource` scores the same as one fed inline
bytes, so the PDFs already uploaded are the right input; the cost is that a
page-count/5 fan-out of synchronous calls replaces a handful of operations.
Ten workers put the three ACs at roughly two hours.

Cost is per page and therefore worth knowing before you run it: $1.50/1000
pages with the first 1000 each month free. `--dry-run` prints the page-exact
bill and stops.

Everything is resumable and nothing is ever re-billed: `upload` skips objects
already in the bucket, and `annotate` skips any five-page window whose
response is already on disk. Re-running a completed run costs nothing. The
window files are the unit of progress precisely so that a kill 300 pages into
a 700-page part does not throw those 300 away. The one thing "already on
disk" does not settle is a window holding a *page-level* error, which arrives
inside a 200 OK and used to be checkpointed as though it had succeeded --
`--retry-failed` re-submits those, and is the only flag here that re-bills
pages deliberately.

Stages (`--stage`, default `all`):
  upload    stream the PDFs out of the raw zips into GCS
  annotate  five pages per synchronous request, one response file per window
  backup    copy the responses up to the bucket, so losing the laptop's copy
            does not mean paying for them twice
"""
from __future__ import annotations

import argparse
import gzip
import json
import io
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VISION_ENDPOINT = "https://vision.googleapis.com/v1"
PRICE_PER_1K = 1.50
FREE_PAGES_PER_MONTH = 1000

# `files:annotate`'s own ceiling, not a tuning knob -- the API rejects a
# sixth page.
PAGES_PER_REQUEST = 5

# Parts OCR'd concurrently. Each is a serial chain of synchronous requests, so
# this is the only parallelism there is. Vision's per-project request quota is
# 1800/minute by default and ten workers come nowhere near it; the real limit
# is politeness and the laptop's sockets.
WORKERS = 10

# A run outlives a token's one-hour life, and re-shelling to gcloud for every
# one of ~2300 requests is its own cost.
TOKEN_TTL_SECONDS = 45 * 60

RETRIES = 5

# Page-level Vision errors worth another attempt. Anything else ("Bad image
# data.", a page box the renderer refuses) fails identically next time and is
# written through, so the connector can name it instead of the run looping.
RETRYABLE_PAGE_ERROR = re.compile(
    r"currently unavailable|internal error|deadline exceeded|try again",
    re.I,
)

_token_lock = threading.Lock()
_token_cache: dict = {"value": None, "expires": 0.0}
_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _token(force: bool = False) -> str:
    """The current access token, re-minted when it ages out or is rejected.

    force is what a 401 passes back in. A cached token is not only an expiry
    risk: gcloud can hand back a token Vision rejects outright
    (ACCESS_TOKEN_TYPE_UNSUPPORTED), and cached under a TTL that reads as
    still-valid, every worker then fails against the same bad value until the
    TTL runs out. A 45-minute unattended run died at 70% that way -- 169
    consecutive 401s, no retry, because a bad token was classified as a
    permanent failure rather than a refreshable one.
    """
    with _token_lock:
        now = time.monotonic()
        if force or _token_cache["value"] is None or now >= _token_cache["expires"]:
            minted = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            # Refuse to cache something that cannot be a bearer token rather
            # than spending the next TTL discovering it one 401 at a time.
            if not minted.startswith("ya29."):
                raise RuntimeError(
                    "gcloud returned something that is not an OAuth access "
                    f"token ({minted[:16]!r}...). Run: gcloud auth login"
                )
            _token_cache["value"] = minted
            _token_cache["expires"] = now + TOKEN_TTL_SECONDS
        return _token_cache["value"]


def _api(method: str, url: str, project: str, body: dict | None = None) -> dict:
    """One Vision call, retried on the failures that are worth retrying.

    429 and 5xx are transient and a run this long will meet both; anything
    else (a malformed request, a missing object) will fail identically on the
    next attempt, so it is raised immediately rather than burning five
    backoffs on it.

    401 is the exception, and it is one this run learned the hard way: a
    cached token that expired or that gcloud minted wrong is refreshable, not
    permanent. It is retried exactly once per call, forcing a new token first,
    so a genuinely revoked credential still fails fast instead of looping.
    """
    data = json.dumps(body).encode() if body is not None else None
    forced = False
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {_token()}")
        # Without this an inline (non-async) Vision call 403s on a
        # user-credential token, quota project unset.
        req.add_header("x-goog-user-project", project)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:800]
            if e.code == 401 and not forced:
                forced = True
                _token(force=True)
                continue          # no backoff: the token, not the service, was bad
            if e.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                raise RuntimeError(f"{method} {url} -> {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == RETRIES - 1:
                raise RuntimeError(f"{method} {url} -> {e}") from None
        time.sleep((2 ** attempt) + random.random())
    raise AssertionError("unreachable")


def _gcs_list(prefix: str) -> set[str]:
    """Object URIs under a gs:// prefix, empty if the prefix doesn't exist."""
    r = subprocess.run(
        ["gcloud", "storage", "ls", "--recursive", prefix],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return set()
    # `ls --recursive` interleaves directory headers ("gs://.../AC287/:") with
    # the objects themselves. Both start with gs://, so the header has to be
    # excluded explicitly -- letting one through submits it to Vision as if it
    # were a file, which fails the request with a 404.
    out = set()
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if not ln.startswith("gs://") or ln.endswith("/") or ln.endswith(":"):
            continue
        out.add(ln)
    return out


def _parts(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as z:
        return sorted(n for n in z.namelist() if n.lower().endswith(".pdf"))


# ---------------------------------------------------------------- page counts

def page_counts(ac: str, zip_path: Path, out_dir: Path) -> dict[str, int]:
    """{part stem: page count} for one AC, cached beside the responses.

    Counted locally rather than learned from the first response of each part:
    it makes the bill exact *before* anything is billed, it makes the window
    plan deterministic (so a resumed run computes the same windows a killed
    one did), and it costs nothing. The cache exists because it means opening
    every PDF in a 2GB zip.
    """
    cache = out_dir / ac / "pages.json"
    if cache.exists():
        return json.loads(cache.read_text())
    import pdfplumber  # a heavy import, and only this stage needs it

    counts: dict[str, int] = {}
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(n for n in z.namelist() if n.lower().endswith(".pdf"))
        for i, n in enumerate(names, 1):
            with pdfplumber.open(io.BytesIO(z.read(n))) as pdf:
                counts[Path(n).stem] = len(pdf.pages)
            if i % 50 == 0:
                _say(f"  {ac}: counted {i}/{len(names)} parts")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(counts, indent=1, sort_keys=True))
    return counts


def _windows(total: int) -> list[list[int]]:
    """Vision page numbers are 1-based; the last window is short, not padded.

    Asking for a page past the end fails the whole request, so the tail is
    trimmed rather than rounded up.
    """
    return [list(range(s, min(s + PAGES_PER_REQUEST - 1, total) + 1))
            for s in range(1, total + 1, PAGES_PER_REQUEST)]


# Gzipped, ~10x. A page of Vision output is ~380KB of JSON and this corpus is
# 11,407 of them, so the plain form is 4.3GB on a laptop that has been down to
# single-digit free GB mid-download. `gzip.open` reads it back in one line.
def _window_name(pages: list[int]) -> str:
    return f"p{pages[0]:04d}-{pages[-1]:04d}.json.gz"


# Reading these files back is states/west_bengal_ocr.py's job, not this
# script's -- window_paths()/load_window() live there beside the parser that
# consumes them, so the naming convention this stage writes and the one the
# connector reads cannot drift apart.


# -------------------------------------------------------------------- stages

def stage_upload(acs, raw_dir: Path, bucket: str, chunk: int = 40) -> None:
    """Stream each AC's PDFs into GCS.

    Extracted in chunks and deleted as it goes: the three zips are ~6GB
    unpacked and this runs on a laptop that has been down to single-digit
    free GB while a download was in flight.
    """
    for ac in acs:
        zip_path = raw_dir / f"{ac}.zip"
        if not zip_path.exists():
            _say(f"{ac}: no zip at {zip_path}, skipping")
            continue
        dest = f"{bucket}/raw/{ac}"
        have = {u.rsplit("/", 1)[-1] for u in _gcs_list(dest + "/")}
        names = _parts(zip_path)
        todo = [n for n in names if Path(n).name not in have]
        _say(f"{ac}: {len(names)} parts, {len(have)} already uploaded, "
             f"{len(todo)} to go")
        with zipfile.ZipFile(zip_path) as z:
            for i in range(0, len(todo), chunk):
                batch = todo[i:i + chunk]
                tmp = Path(tempfile.mkdtemp(prefix=f"ocr_{ac}_"))
                try:
                    for n in batch:
                        (tmp / Path(n).name).write_bytes(z.read(n))
                    subprocess.run(
                        ["gcloud", "storage", "cp"]
                        + [str(tmp / Path(n).name) for n in batch]
                        + [dest + "/"],
                        check=True, capture_output=True, text=True,
                    )
                    _say(f"  {ac}: uploaded {i + len(batch)}/{len(todo)}")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


def _annotate_window(uri: str, pages: list[int], project: str) -> list[dict]:
    """The page responses for one window, unwrapped.

    `files:annotate` nests them one level deeper than the async path's output
    shards do; unwrapping here means the file written to disk has the shard
    shape `parse_response_file` already reads, and the connector never learns
    which endpoint produced it.
    """
    body = {"requests": [{
        "inputConfig": {"gcsSource": {"uri": uri}, "mimeType": "application/pdf"},
        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
        # Bengali. Vision autodetects, but the hint measurably helps on the
        # conjunct-heavy names this corpus is entirely made of.
        "imageContext": {"languageHints": ["bn"]},
        "pages": pages,
    }]}
    for attempt in range(RETRIES):
        r = _api("POST", f"{VISION_ENDPOINT}/files:annotate", project, body)
        inner = r.get("responses", [{}])[0]
        if "error" in inner:
            raise RuntimeError(f"{uri} pages {pages[0]}-{pages[-1]}: {inner['error']}")
        responses = inner.get("responses", [])
        # A page-level error arrives inside a 200 OK, so _api's retry never
        # sees it. Writing the window anyway checkpoints the failure forever:
        # the next run skips the file because it exists, and those pages'
        # electors are missing from the built AC with nothing in the log to
        # say so. Found after the fact -- 10 pages of AC287 and 21 of AC291
        # are on disk holding only "The service is currently unavailable.".
        transient = [p for p, resp in zip(pages, responses)
                     if RETRYABLE_PAGE_ERROR.search(resp.get("error", {}).get("message", ""))]
        if not transient or attempt == RETRIES - 1:
            return responses
        _say(f"    retrying {uri} pages {transient} ({len(transient)} transient error(s))")
        time.sleep((2 ** attempt) + random.random())
    raise AssertionError("unreachable")


def _existing_window(part_dir: Path, pages: list[int]) -> Path | None:
    dest = part_dir / _window_name(pages)
    for path in (dest, dest.with_suffix("")):   # ".json" from before gzip
        if path.exists():
            return path
    return None


def _window_is_poisoned(path: Path) -> bool:
    """True if a window already on disk holds a page error worth retrying.

    Windows written before _annotate_window learned to retry page-level
    errors have those errors baked in, and "the file exists" is the only
    thing the resume check ever asked. --retry-failed re-asks properly.
    Unreadable or truncated counts as poisoned too: the alternative is
    skipping it forever on the strength of its filename.
    """
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            responses = json.load(fh).get("responses", [])
    except (OSError, ValueError):
        return True
    return any(RETRYABLE_PAGE_ERROR.search(r.get("error", {}).get("message", ""))
               for r in responses)


def _window_pending(part_dir: Path, pages: list[int], retry_failed: bool) -> bool:
    """Whether this window still needs OCR. The cost estimate and the work
    itself both go through here, so a run cannot quote one number and do
    another."""
    path = _existing_window(part_dir, pages)
    if path is None:
        return True
    return retry_failed and _window_is_poisoned(path)


def _annotate_part(ac: str, stem: str, total: int, bucket: str, project: str,
                   out_dir: Path, retry_failed: bool = False) -> tuple[int, int]:
    """OCR whatever of one part is not already on disk. -> (billed, skipped)."""
    part_dir = out_dir / ac / stem
    part_dir.mkdir(parents=True, exist_ok=True)
    uri = f"{bucket}/raw/{ac}/{stem}.pdf"
    billed = skipped = 0
    for pages in _windows(total):
        dest = part_dir / _window_name(pages)
        if not _window_pending(part_dir, pages, retry_failed):
            skipped += len(pages)
            continue
        responses = _annotate_window(uri, pages, project)
        # Written whole, then moved: a half-written window file that still
        # looks present is the one way this could silently skip real pages on
        # the next run.
        tmp = dest.with_name(dest.name + ".partial")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"responses": responses}, fh)
        tmp.replace(dest)
        billed += len(pages)
    return billed, skipped


def stage_annotate(acs, raw_dir: Path, bucket: str, project: str, out_dir: Path,
                   dry_run: bool, limit: int | None, workers: int,
                   retry_failed: bool = False) -> None:
    plan: list[tuple[str, str, int]] = []
    for ac in acs:
        zip_path = raw_dir / f"{ac}.zip"
        if not zip_path.exists():
            _say(f"{ac}: no zip at {zip_path}, skipping")
            continue
        counts = page_counts(ac, zip_path, out_dir)
        plan += [(ac, stem, n) for stem, n in sorted(counts.items())]

    pending = 0
    for ac, stem, total in plan:
        part_dir = out_dir / ac / stem
        pending += sum(len(p) for p in _windows(total)
                       if _window_pending(part_dir, p, retry_failed))

    pages = sum(n for _, _, n in plan)
    _say(f"{len(plan)} parts, {pages} pages, {pending} not yet OCR'd")
    billable = max(pending - FREE_PAGES_PER_MONTH, 0)
    _say(f"bill for this run: {pending} pages, {FREE_PAGES_PER_MONTH} free/month "
         f"=> ${billable * PRICE_PER_1K / 1000:.2f} "
         f"(a full re-OCR would be ${max(pages - FREE_PAGES_PER_MONTH, 0) * PRICE_PER_1K / 1000:.2f})")
    if dry_run:
        _say("--dry-run: nothing submitted")
        return
    if not pending:
        return
    if limit:
        plan = plan[:limit]
        _say(f"--limit {limit}: only the first {len(plan)} parts this run")

    started = time.monotonic()
    billed = done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_annotate_part, ac, stem, total, bucket, project,
                               out_dir, retry_failed): (ac, stem)
                   for ac, stem, total in plan}
        for fut in as_completed(futures):
            ac, stem = futures[fut]
            done += 1
            try:
                b, _ = fut.result()
            except Exception as e:               # one bad part must not end the run
                _say(f"  !! {ac}/{stem}: {e}")
                continue
            billed += b
            if done % 10 == 0 or done == len(futures):
                rate = billed / max(time.monotonic() - started, 1e-9)
                _say(f"  {done}/{len(futures)} parts, {billed} pages billed "
                     f"({rate * 60:.0f}/min)")
    _say(f"annotate done: {billed} pages billed this run")


def stage_backup(acs, bucket: str, out_dir: Path) -> None:
    """Push the responses up to the bucket.

    Not where the connector reads them from -- purely so that losing the
    laptop's copy is not a second $15.61.
    """
    for ac in acs:
        src = out_dir / ac
        if not src.exists():
            continue
        subprocess.run(
            ["gcloud", "storage", "rsync", "--recursive",
             str(src), f"{bucket}/out/{ac}/"],
            check=True,
        )
        _say(f"{ac}: {sum(1 for _ in src.rglob('p*.json*'))} response files backed up")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--acs", default="AC287,AC291,AC294",
                   help="comma-separated AC codes (default: the WB page-scan three)")
    p.add_argument("--raw-dir", type=Path, default=Path("data/raw/west_bengal"))
    p.add_argument("--out-dir", type=Path, default=Path("data/raw/west_bengal/ocr"))
    p.add_argument("--bucket", default="gs://oldvoterlist-ocr-work")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT", "oldvoterlist-prod"))
    p.add_argument("--stage", default="all",
                   choices=["upload", "annotate", "backup", "all"])
    p.add_argument("--workers", type=int, default=WORKERS)
    p.add_argument("--limit", type=int,
                   help="OCR at most this many parts this run -- a paid smoke "
                        "test before committing to the whole corpus")
    p.add_argument("--retry-failed", action="store_true",
                   help="re-OCR windows already on disk that hold a retryable "
                        "page error (transient Vision failures were checkpointed "
                        "as done before this script retried them). Pair with "
                        "--dry-run first: it re-bills those pages.")
    p.add_argument("--dry-run", action="store_true",
                   help="report the page-exact bill, then stop")
    a = p.parse_args(argv)

    acs = [s.strip() for s in a.acs.split(",") if s.strip()]
    a.out_dir.mkdir(parents=True, exist_ok=True)

    if a.stage in ("upload", "all") and not a.dry_run:
        stage_upload(acs, a.raw_dir, a.bucket)
    if a.stage in ("annotate", "all", "upload"):
        # `upload --dry-run` should still cost out what it is uploading for.
        if a.stage != "upload" or a.dry_run:
            stage_annotate(acs, a.raw_dir, a.bucket, a.project, a.out_dir,
                           a.dry_run, a.limit, a.workers, a.retry_failed)
    if a.stage in ("backup", "all") and not a.dry_run:
        stage_backup(acs, a.bucket, a.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
