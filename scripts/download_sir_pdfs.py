"""
Download old SIR roll PDFs for any state, bundled as one ZIP per AC.

Supports all 29 ECI-pattern states plus the 4 special states (Bihar,
Gujarat, Chandigarh, D&NH). Each AC's part PDFs are downloaded and
bundled into a single ZIP at data/raw/{state_id}/{ac_code}.zip with
a manifest.json recording what was fetched.

Usage:
    # Download one state
    download_sir_pdfs.py --state telangana

    # Download specific ACs
    download_sir_pdfs.py --state goa --ac 1,2,3

    # Download first 5 ACs (for testing)
    download_sir_pdfs.py --state delhi --limit 5

    # Adjust rate limit
    download_sir_pdfs.py --state tamil_nadu --rate 0.3

    # Force re-download
    download_sir_pdfs.py --state sikkim --force

    # List available states
    download_sir_pdfs.py --list
"""
import argparse
import concurrent.futures
import io
import json
import os
import ssl
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

META_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "states", "meta")

# SSL context for sites with cert issues (Chandigarh, D&NH)
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# ── ECI-pattern states: URL constructed from f-server + state code ───────
ECI_PATTERN = "https://www.eci.gov.in/sir/{fvar}/{state_cd}/data/OLDSIRROLL/{state_cd}/{ac_no}/{state_cd}_{ac_no}_{part_no}.pdf"

ECI_STATES = {
    "andhra_pradesh":   ("S01", "f1"), "arunachal_pradesh": ("S02", "f1"),
    "assam":            ("S03", "f1"), "goa":               ("S05", "f1"),
    "himachal_pradesh": ("S08", "f1"),
    "kerala":           ("S11", "f2"), "madhya_pradesh":    ("S12", "f2"),
    "maharashtra":      ("S13", "f2"), "manipur":           ("S14", "f2"),
    "meghalaya":        ("S15", "f2"), "mizoram":           ("S16", "f2"),
    "nagaland":         ("S17", "f2"), "odisha":            ("S18", "f2"),
    "punjab":           ("S19", "f2"),
    "rajasthan":        ("S20", "f3"), "sikkim":            ("S21", "f3"),
    "tamil_nadu":       ("S22", "f3"), "tripura":           ("S23", "f3"),
    "uttar_pradesh":    ("S24", "f3"), "chhattisgarh":      ("S26", "f3"),
    "jharkhand":        ("S27", "f3"), "uttarakhand":       ("S28", "f3"),
    "telangana":        ("S29", "f4"), "andaman_nicobar":   ("U01", "f4"),
    "delhi":            ("U05", "f4"), "lakshadweep":       ("U06", "f4"),
    "puducherry":       ("U07", "f4"), "ladakh":            ("U09", "f4"),
}

# ── Special states ───────────────────────────────────────────────────────
SPECIAL_STATES = {"bihar", "gujarat", "chandigarh", "dadra_nagar_haveli_daman_diu"}

ALL_STATES = set(ECI_STATES.keys()) | SPECIAL_STATES

ROLL_YEARS = {
    "andhra_pradesh": 2002, "arunachal_pradesh": 2006, "assam": 2005,
    "bihar": 2003, "goa": 2002, "gujarat": 2002, "himachal_pradesh": 2002,
    "kerala": 2002, "madhya_pradesh": 2003, "maharashtra": 2002,
    "manipur": 2005, "meghalaya": 2005, "mizoram": 2005, "nagaland": 2005,
    "odisha": 2002, "punjab": 2003, "rajasthan": 2002, "sikkim": 2002,
    "tamil_nadu": 2005, "tripura": 2005, "uttar_pradesh": 2003,
    "chhattisgarh": 2003, "jharkhand": 2003, "uttarakhand": 2003,
    "telangana": 2002, "andaman_nicobar": 2002, "delhi": 2002,
    "lakshadweep": 2002, "puducherry": 2002, "ladakh": 2005,
    "chandigarh": 2002, "dadra_nagar_haveli_daman_diu": 2002,
}


def _download(url, timeout=30):
    """Download a URL, return bytes or None."""
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
            return resp.read()
    except Exception:
        return None


def _load_meta(state_id):
    """Load the state's AC meta JSON."""
    meta_file = os.path.join(META_DIR, f"{state_id}_ac_meta.json")
    if not os.path.exists(meta_file):
        # Karnataka uses ac_meta.json
        if state_id == "karnataka":
            meta_file = os.path.join(META_DIR, "ac_meta.json")
        else:
            raise FileNotFoundError(f"Meta file not found: {meta_file}")
    with open(meta_file, encoding="utf-8") as f:
        return json.load(f)


def _ac_code(state_id, ac_no):
    """Generate AC code string for file naming."""
    if state_id in ECI_STATES:
        state_cd = ECI_STATES[state_id][0]
        return f"{state_cd}_AC{ac_no:03d}"
    return f"AC{ac_no:03d}"


# ── ECI-pattern downloader ──────────────────────────────────────────────
def download_eci_ac(state_id, ac, rate):
    """Download all part PDFs for one AC, return ZIP bytes."""
    state_cd, fvar = ECI_STATES[state_id]
    ac_no = ac["ac_no"]
    part_numbers = ac.get("part_numbers", list(range(1, ac.get("total_parts", 0) + 1)))

    fetched, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for part_no in part_numbers:
            url = ECI_PATTERN.format(fvar=fvar, state_cd=state_cd, ac_no=ac_no, part_no=part_no)
            data = _download(url)
            if data and data[:4] == b"%PDF":
                zf.writestr(f"part{part_no:04d}.pdf", data)
                fetched.append(part_no)
            else:
                missing.append(part_no)
            time.sleep(rate)

        zf.writestr("manifest.json", json.dumps({
            "state_id": state_id,
            "state_cd": state_cd,
            "ac_no": ac_no,
            "ac_name": ac.get("ac_name", ""),
            "district": ac.get("district_name", ""),
            "roll_year": ROLL_YEARS.get(state_id),
            "parts_listed": part_numbers,
            "fetched": fetched,
            "missing": missing,
        }, indent=1))

    if not fetched:
        return None, 0, len(part_numbers)
    return buf.getvalue(), len(fetched), len(missing)


# ── Bihar: uses oldPdfUrl from meta ─────────────────────────────────────
def download_bihar_ac(ac, rate):
    parts = ac.get("parts", [])
    fetched, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in parts:
            part_no = p["part_no"]
            url = p.get("pdf_url", "")
            if not url:
                missing.append(part_no)
                continue
            # URL has spaces, need to encode
            url = url.replace(" ", "%20")
            data = _download(url)
            if data and data[:4] == b"%PDF":
                zf.writestr(f"part{part_no:04d}.pdf", data)
                fetched.append(part_no)
            else:
                missing.append(part_no)
            time.sleep(rate)

        zf.writestr("manifest.json", json.dumps({
            "state_id": "bihar",
            "ac_no": ac["ac_no"],
            "ac_name": ac.get("ac_name", ""),
            "district": ac.get("district_name", ""),
            "roll_year": 2003,
            "fetched": fetched, "missing": missing,
        }, indent=1))

    if not fetched:
        return None, 0, len(parts)
    return buf.getvalue(), len(fetched), len(missing)


# ── Gujarat: single ZIP per AC (direct download) ────────────────────────
def download_gujarat_ac(ac, rate):
    url = ac.get("zip_url", "")
    if not url:
        return None, 0, 1
    data = _download(url, timeout=120)
    if data and len(data) > 100:
        return data, 1, 0
    return None, 0, 1


# ── Chandigarh: PDFs from CEO site ─────────────────────────────────────
def download_chandigarh_ac(ac, rate):
    parts = ac.get("parts", [])
    fetched, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in parts:
            part_no = p["part_no"]
            url = p.get("pdf_url", "")
            if not url:
                missing.append(part_no)
                continue
            data = _download(url)
            if data and data[:4] == b"%PDF":
                zf.writestr(f"part{part_no:04d}.pdf", data)
                fetched.append(part_no)
            else:
                missing.append(part_no)
            time.sleep(rate)

        zf.writestr("manifest.json", json.dumps({
            "state_id": "chandigarh",
            "ac_no": ac["ac_no"],
            "ac_name": ac.get("ac_name", ""),
            "district": ac.get("district_name", ""),
            "roll_year": 2002,
            "fetched": fetched, "missing": missing,
        }, indent=1))

    if not fetched:
        return None, 0, len(parts)
    return buf.getvalue(), len(fetched), len(missing)


# ── D&NH: PDFs from CEO site ───────────────────────────────────────────
def download_dnh_ac(ac, rate):
    parts = ac.get("parts", [])
    fetched, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in parts:
            part_no = p["part_no"]
            url = p.get("pdf_url", "")
            if not url:
                missing.append(part_no)
                continue
            data = _download(url)
            if data and data[:4] == b"%PDF":
                zf.writestr(f"part{part_no:04d}.pdf", data)
                fetched.append(part_no)
            else:
                missing.append(part_no)
            time.sleep(rate)

        zf.writestr("manifest.json", json.dumps({
            "state_id": "dadra_nagar_haveli_daman_diu",
            "ac_no": ac["ac_no"],
            "ac_name": ac.get("ac_name", ""),
            "district": ac.get("district_name", ""),
            "roll_year": 2002,
            "fetched": fetched, "missing": missing,
        }, indent=1))

    if not fetched:
        return None, 0, len(parts)
    return buf.getvalue(), len(fetched), len(missing)


def _download_one_ac(state_id, ac, rate, out_dir, force):
    """Download a single AC. Returns (status, ac_no, message)."""
    ac_no = ac["ac_no"]
    ac_name = ac.get("ac_name", "")
    out_path = os.path.join(out_dir, f"AC{ac_no:03d}.zip")

    if os.path.exists(out_path) and not force:
        return "skipped", ac_no, ""

    try:
        if state_id in ECI_STATES:
            raw, n_ok, n_miss = download_eci_ac(state_id, ac, rate)
        elif state_id == "bihar":
            raw, n_ok, n_miss = download_bihar_ac(ac, rate)
        elif state_id == "gujarat":
            raw, n_ok, n_miss = download_gujarat_ac(ac, rate)
        elif state_id == "chandigarh":
            raw, n_ok, n_miss = download_chandigarh_ac(ac, rate)
        elif state_id == "dadra_nagar_haveli_daman_diu":
            raw, n_ok, n_miss = download_dnh_ac(ac, rate)
        else:
            return "failed", ac_no, f"No downloader for {state_id}"

        if raw is None:
            return "failed", ac_no, "no parts fetched"

        with open(out_path, "wb") as f:
            f.write(raw)
        size_mb = len(raw) / 1024 / 1024
        msg = f"AC{ac_no:03d} {ac_name} -> {n_ok} parts, {size_mb:.1f} MB"
        if n_miss:
            msg += f" ({n_miss} missing)"
        return "ok", ac_no, msg

    except Exception as e:
        return "failed", ac_no, str(e)


def download_state(state_id, ac_filter=None, limit=None, rate=0.3, force=False, out_base="data/raw", workers=1):
    meta = _load_meta(state_id)
    out_dir = os.path.join(out_base, state_id)
    os.makedirs(out_dir, exist_ok=True)

    # Filter ACs if requested
    if ac_filter:
        wanted = set(int(x) for x in ac_filter.split(","))
        meta = [ac for ac in meta if ac["ac_no"] in wanted]
    if limit:
        meta = meta[:limit]

    total_parts = sum(
        ac.get("total_parts", len(ac.get("parts", ac.get("part_numbers", []))))
        for ac in meta
    )
    print(f"\n{'='*60}")
    print(f"{state_id}: {len(meta)} ACs, ~{total_parts} parts to download (workers={workers})")
    print(f"Output: {out_dir}")
    print(f"{'='*60}", flush=True)

    ok, skipped, failed = 0, 0, []

    if workers <= 1:
        for i, ac in enumerate(meta, 1):
            status, ac_no, msg = _download_one_ac(state_id, ac, rate, out_dir, force)
            if status == "skipped":
                skipped += 1
            elif status == "ok":
                ok += 1
                print(f"  [{i}/{len(meta)}] {msg}", flush=True)
            else:
                failed.append((ac_no, msg))
                print(f"  [{i}/{len(meta)}] AC{ac_no:03d} FAILED: {msg}", flush=True)
    else:
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_download_one_ac, state_id, ac, rate, out_dir, force): ac
                for ac in meta
            }
            for future in concurrent.futures.as_completed(futures):
                done += 1
                status, ac_no, msg = future.result()
                if status == "skipped":
                    skipped += 1
                elif status == "ok":
                    ok += 1
                    print(f"  [{done}/{len(meta)}] {msg}", flush=True)
                else:
                    failed.append((ac_no, msg))
                    print(f"  [{done}/{len(meta)}] AC{ac_no:03d} FAILED: {msg}", flush=True)

    print(f"\n{state_id}: {ok} downloaded, {skipped} already present, {len(failed)} failed.")
    if failed:
        print(f"  Failed ACs: {', '.join(str(ac) for ac, _ in failed)}")
    return ok, skipped, len(failed)


def main():
    ap = argparse.ArgumentParser(description="Download old SIR roll PDFs for any state")
    ap.add_argument("--state", help="state_id to download (e.g. telangana, goa, delhi)")
    ap.add_argument("--ac", default=None, help="AC numbers to fetch, comma-separated (e.g. 1,2,3)")
    ap.add_argument("--limit", type=int, default=None, help="only fetch first N ACs")
    ap.add_argument("--rate", type=float, default=0.3, help="seconds between PDF requests (default 0.3)")
    ap.add_argument("--workers", type=int, default=1, help="parallel AC downloads (default 1)")
    ap.add_argument("--force", action="store_true", help="re-download even if file exists")
    ap.add_argument("--out-dir", default="data/raw", help="base output directory")
    ap.add_argument("--list", action="store_true", help="list available states and exit")
    args = ap.parse_args()

    if args.list:
        print("Available states:")
        for s in sorted(ALL_STATES):
            year = ROLL_YEARS.get(s, "?")
            src = "ECI" if s in ECI_STATES else "special"
            print(f"  {s:<40} year={year}  source={src}")
        return

    if not args.state:
        ap.error("--state is required (use --list to see available states)")

    if args.state not in ALL_STATES:
        print(f"Unknown state: {args.state}")
        print(f"Available: {', '.join(sorted(ALL_STATES))}")
        sys.exit(1)

    download_state(args.state, ac_filter=args.ac, limit=args.limit,
                   rate=args.rate, force=args.force, out_base=args.out_dir,
                   workers=args.workers)


if __name__ == "__main__":
    main()
