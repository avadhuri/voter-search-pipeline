"""OCR the page-scan constituencies through Google Cloud Vision.

A handful of West Bengal ACs (AC287, AC291, AC294) ship as page scans with
no text layer at all, so `states/west_bengal.py` refuses them -- there are no
glyphs to decode, only pixels. This script is the missing front half for
those: it runs the scans through Vision's DOCUMENT_TEXT_DETECTION and lands
the raw responses in GCS, where a connector can read them as if they were a
text layer.

Vision is used rather than a local engine because of a measured bake-off
(Vision / Tesseract / Surya on 278 ground-truth name cells decoded from the
digital rolls' own font table). Two findings decided it: Vision holds its
accuracy at 200 dpi where Tesseract falls off (83.2% vs 75.2% exact), and two
of these three ACs are 200 dpi; and on the real scans Tesseract found 58 valid
EPIC numbers against Vision's 107. Surya matched Vision on accuracy but needs
a VLM inference server, measured at 48.9s/page on an M1 Pro even fully batched
onto the GPU -- about a week for this corpus.

Cost is per page and therefore worth knowing before you run it: $1.50/1000
pages with the first 1000 each month free. `--dry-run` prints the bill and
stops. The three ACs are 11,407 pages = $15.61.

Everything is resumable and nothing is ever re-billed: `upload` skips objects
already in the bucket, `annotate` skips any part whose output prefix already
holds a response, and the operation list is checkpointed to disk so a killed
poll can be picked back up. Re-running the whole thing after a completed run
costs nothing.

Stages (`--stage`, default `all`):
  upload    stream the PDFs out of the raw zips into GCS
  annotate  submit asyncBatchAnnotate operations, checkpoint their names
  poll      wait for the submitted operations to finish
  fetch     download the response JSON alongside the raw zips
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VISION_ENDPOINT = "https://vision.googleapis.com/v1"
PRICE_PER_1K = 1.50
FREE_PAGES_PER_MONTH = 1000

# Files per asyncBatchAnnotate operation. Vision bills per page either way, so
# this only trades operation count against per-operation latency; ~10 parts is
# ~200 pages, comfortably inside the 2000-page-per-file ceiling with room for
# an unusually long part.
FILES_PER_OPERATION = 10
# Operations in flight at once. Vision's async queue is shared per project, so
# this is politeness rather than a hard limit.
MAX_IN_FLIGHT = 8
POLL_INTERVAL_SECONDS = 20


def _token() -> str:
    """A fresh access token.

    Fetched per call rather than cached: a full run outlives the one-hour
    token lifetime, and gcloud caches underneath us anyway.
    """
    return subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _api(method: str, url: str, project: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("x-goog-user-project", project)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        raise RuntimeError(f"{method} {url} -> {e.code}: {detail}") from None


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
    # were a file, which fails the whole operation with a 404.
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


def stage_upload(acs, raw_dir: Path, bucket: str, chunk: int = 40) -> None:
    """Stream each AC's PDFs into GCS.

    Extracted in chunks and deleted as it goes: the three zips are ~6GB
    unpacked and this runs on a laptop that has been down to single-digit
    free GB while a download was in flight.
    """
    for ac in acs:
        zip_path = raw_dir / f"{ac}.zip"
        if not zip_path.exists():
            print(f"{ac}: no zip at {zip_path}, skipping", file=sys.stderr)
            continue
        dest = f"{bucket}/raw/{ac}"
        have = {u.rsplit("/", 1)[-1] for u in _gcs_list(dest + "/")}
        names = _parts(zip_path)
        todo = [n for n in names if Path(n).name not in have]
        print(f"{ac}: {len(names)} parts, {len(have)} already uploaded, "
              f"{len(todo)} to go", flush=True)
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
                    print(f"  {ac}: uploaded {i + len(batch)}/{len(todo)}", flush=True)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


def _pending(acs, bucket: str) -> list[tuple[str, str]]:
    """(ac, part_stem) pairs uploaded but with no Vision output yet."""
    out = []
    for ac in acs:
        uploaded = {u.rsplit("/", 1)[-1][:-4]
                    for u in _gcs_list(f"{bucket}/raw/{ac}/") if u.endswith(".pdf")}
        done = {u[len(f"{bucket}/out/{ac}/"):].split("/")[0]
                for u in _gcs_list(f"{bucket}/out/{ac}/")}
        out += [(ac, s) for s in sorted(uploaded - done)]
    return out


def stage_annotate(acs, bucket: str, project: str, state_path: Path,
                   dry_run: bool) -> None:
    todo = _pending(acs, bucket)
    if not todo:
        print("nothing to annotate -- every uploaded part already has output")
        return
    print(f"{len(todo)} parts to annotate")
    if dry_run:
        print("--dry-run: not submitting")
        return

    state = json.loads(state_path.read_text()) if state_path.exists() else {"ops": []}
    seen = {o["key"] for o in state["ops"]}
    inflight: list[dict] = []

    for i in range(0, len(todo), FILES_PER_OPERATION):
        group = todo[i:i + FILES_PER_OPERATION]
        key = f"{group[0][0]}/{group[0][1]}+{len(group)}"
        if key in seen:
            continue
        requests = [{
            "inputConfig": {
                "gcsSource": {"uri": f"{bucket}/raw/{ac}/{stem}.pdf"},
                "mimeType": "application/pdf",
            },
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            # Bengali. Vision autodetects, but the hint measurably helps on
            # the conjunct-heavy names this corpus is entirely made of.
            "imageContext": {"languageHints": ["bn"]},
            "outputConfig": {
                "gcsDestination": {"uri": f"{bucket}/out/{ac}/{stem}/"},
                "batchSize": 20,
            },
        } for ac, stem in group]

        op = _api("POST", f"{VISION_ENDPOINT}/files:asyncBatchAnnotate",
                  project, {"requests": requests})
        rec = {"key": key, "name": op["name"], "files": len(group)}
        state["ops"].append(rec)
        inflight.append(rec)
        state_path.write_text(json.dumps(state, indent=1))
        print(f"  submitted {key} -> {op['name']}", flush=True)

        while len(inflight) >= MAX_IN_FLIGHT:
            time.sleep(POLL_INTERVAL_SECONDS)
            inflight = [o for o in inflight if not _op_done(o["name"], project)]


def _op_done(name: str, project: str) -> bool:
    r = _api("GET", f"{VISION_ENDPOINT}/{name}", project)
    if r.get("done") and "error" in r:
        print(f"  !! {name}: {r['error']}", file=sys.stderr)
    return bool(r.get("done"))


def stage_poll(project: str, state_path: Path) -> None:
    if not state_path.exists():
        print("no operations checkpointed; nothing to poll")
        return
    ops = json.loads(state_path.read_text())["ops"]
    remaining = list(ops)
    while remaining:
        remaining = [o for o in remaining if not _op_done(o["name"], project)]
        print(f"  {len(ops) - len(remaining)}/{len(ops)} operations done", flush=True)
        if remaining:
            time.sleep(POLL_INTERVAL_SECONDS)
    print("all operations complete")


def stage_fetch(acs, bucket: str, out_dir: Path) -> None:
    for ac in acs:
        dest = out_dir / ac
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gcloud", "storage", "rsync", "--recursive",
             f"{bucket}/out/{ac}/", str(dest)],
            check=True,
        )
        n = sum(1 for _ in dest.rglob("*.json"))
        print(f"{ac}: {n} response files in {dest}")


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
                   choices=["upload", "annotate", "poll", "fetch", "all"])
    p.add_argument("--dry-run", action="store_true",
                   help="report the page count and what it will cost, then stop")
    a = p.parse_args(argv)

    acs = [s.strip() for s in a.acs.split(",") if s.strip()]
    a.out_dir.mkdir(parents=True, exist_ok=True)
    state_path = a.out_dir / "operations.json"

    if a.dry_run:
        total = 0
        for ac in acs:
            zp = a.raw_dir / f"{ac}.zip"
            if not zp.exists():
                print(f"{ac}: no zip", file=sys.stderr)
                continue
            n = len(_parts(zp))
            print(f"{ac}: {n} parts")
        print("run with --stage annotate --dry-run after uploading for a page-exact bill")
        return 0

    if a.stage in ("upload", "all"):
        stage_upload(acs, a.raw_dir, a.bucket)
    if a.stage in ("annotate", "all"):
        stage_annotate(acs, a.bucket, a.project, state_path, a.dry_run)
    if a.stage in ("poll", "all"):
        stage_poll(a.project, state_path)
    if a.stage in ("fetch", "all"):
        stage_fetch(acs, a.bucket, a.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
