"""
Generate states/meta/{state}_ac_meta.json for all states except
Karnataka, West Bengal, Haryana, and J&K (which already have connectors or no data).

For ECI-hosted states (29): stores AC/part metadata; connector constructs URL
from pattern https://www.eci.gov.in/sir/{fN}/{stateCd}/data/OLDSIRROLL/...

For special states (Bihar, Gujarat, Chandigarh, D&NH): stores actual PDF/ZIP
URLs per part since they can't be constructed from a single pattern.

Usage:
    python scripts/generate_meta_json.py              # all 30 states
    python scripts/generate_meta_json.py S22           # single state
    python scripts/generate_meta_json.py S04 S06       # specific states
"""
import json
import os
import sys
import time
import urllib.request
from collections import Counter


def _assert_no_duplicate_acs(meta, state_slug):
    """Fail fast if any ac_no appears more than once in the generated meta."""
    dupes = {k: v for k, v in Counter(a["ac_no"] for a in meta).items() if v > 1}
    if dupes:
        raise ValueError(
            f"{state_slug}: duplicate ac_no values in generated meta: {dupes}. "
            f"Total entries {len(meta)}, unique ACs {len(set(a['ac_no'] for a in meta))}."
        )
from base64 import b64encode

API_BASE = "https://gateway-voters.eci.gov.in/api/v1/citizen/sir"
HEADERS = {
    "applicationName": "VSP",
    "channelidobo": "VSP",
    "PLATFORM-TYPE": "WEB",
}
META_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "states", "meta")

# Roll years from our PDF analysis
ROLL_YEARS = {
    "S01": 2002, "S02": 2006, "S03": 2005, "S04": 2003, "S05": 2002,
    "S06": 2002, "S08": 2002, "S11": 2002, "S12": 2003, "S13": 2002,
    "S14": 2005, "S15": 2005, "S16": 2005, "S17": 2005, "S18": 2002,
    "S19": 2003, "S20": 2002, "S21": 2002, "S22": 2005, "S23": 2005,
    "S24": 2003, "S26": 2003, "S27": 2003, "S28": 2003, "S29": 2002,
    "U01": 2002, "U02": 2002, "U03": 2002, "U05": 2002, "U06": 2002,
    "U07": 2002, "U09": 2005,
}

# ── ECI-pattern states: connector constructs URL at runtime ──────────────
ECI_STATES = {
    # f1
    "S01": ("Andhra Pradesh", "andhra_pradesh", "f1"),
    "S02": ("Arunachal Pradesh", "arunachal_pradesh", "f1"),
    "S03": ("Assam", "assam", "f1"),
    "S05": ("Goa", "goa", "f1"),
    "S08": ("Himachal Pradesh", "himachal_pradesh", "f1"),
    # f2
    "S11": ("Kerala", "kerala", "f2"),
    "S12": ("Madhya Pradesh", "madhya_pradesh", "f2"),
    "S13": ("Maharashtra", "maharashtra", "f2"),
    "S14": ("Manipur", "manipur", "f2"),
    "S15": ("Meghalaya", "meghalaya", "f2"),
    "S16": ("Mizoram", "mizoram", "f2"),
    "S17": ("Nagaland", "nagaland", "f2"),
    "S18": ("Odisha", "odisha", "f2"),
    "S19": ("Punjab", "punjab", "f2"),
    # f3
    "S20": ("Rajasthan", "rajasthan", "f3"),
    "S21": ("Sikkim", "sikkim", "f3"),
    "S22": ("Tamil Nadu", "tamil_nadu", "f3"),
    "S23": ("Tripura", "tripura", "f3"),
    "S24": ("Uttar Pradesh", "uttar_pradesh", "f3"),
    "S26": ("Chhattisgarh", "chhattisgarh", "f3"),
    "S27": ("Jharkhand", "jharkhand", "f3"),
    "S28": ("Uttarakhand", "uttarakhand", "f3"),
    # f4
    "U01": ("Andaman & Nicobar Islands", "andaman_nicobar", "f4"),
    "U05": ("NCT OF Delhi", "delhi", "f4"),
    "U06": ("Lakshadweep", "lakshadweep", "f4"),
    "U07": ("Puducherry", "puducherry", "f4"),
    "U09": ("Ladakh", "ladakh", "f4"),
    "S29": ("Telangana", "telangana", "f4"),
}

# ── Special states: store actual URLs per part ───────────────────────────
SPECIAL_STATES = {
    "S04": ("Bihar", "bihar"),
    "S06": ("Gujarat", "gujarat"),
    "U02": ("Chandigarh", "chandigarh"),
    "U03": ("Dadra & Nagar Haveli and Daman & Diu", "dadra_nagar_haveli_daman_diu"),
}

SKIP_STATES = {"S07", "S10", "S25", "U08"}  # Haryana, Karnataka, WB, J&K

ALL_STATES = {**{k: v[:2] for k, v in ECI_STATES.items()}, **SPECIAL_STATES}


def api_get(endpoint, state_cd, params="", retries=3):
    url = f"{API_BASE}/{endpoint}"
    if params:
        url += f"?{params}"
    headers = {**HEADERS, "state": state_cd}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
                return data.get("payload", data)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                print(f"    FAILED {endpoint} {state_cd} {params}: {e}")
                return []


def get_districts(state_cd):
    payload = api_get("getDistrict", state_cd)
    if not isinstance(payload, list):
        return {}
    return {d["districtNo"]: d["distName"] for d in payload}


def get_assemblies(state_cd):
    payload = api_get("getAsmbly", state_cd)
    return payload if isinstance(payload, list) else []


def get_parts(state_cd, ac_no):
    payload = api_get("getPartByAc", state_cd, f"Asmbly={ac_no}")
    return payload if isinstance(payload, list) else []


# ── ECI-pattern meta: part_numbers list, connector constructs URL ────────
def build_eci_meta(state_cd):
    label, slug, fvar = ECI_STATES[state_cd]
    print(f"\n  {state_cd} {label} (ECI {fvar})")

    dist_map = get_districts(state_cd)
    time.sleep(0.3)
    assemblies = get_assemblies(state_cd)
    time.sleep(0.3)
    print(f"    Districts: {len(dist_map)}, ACs: {len(assemblies)}")

    meta = []
    for i, ac in enumerate(assemblies):
        ac_no = ac["acNo"]
        ac_name = ac.get("acName", "")
        dist_no = ac.get("distNo", 0)
        dist_name = dist_map.get(dist_no, f"District {dist_no}")

        parts = get_parts(state_cd, ac_no)
        part_numbers = sorted(p["partNumber"] for p in parts)

        meta.append({
            "ac_no": ac_no,
            "ac_name": ac_name,
            "district_name": dist_name,
            "district_no": dist_no,
            "total_parts": len(part_numbers),
            "part_numbers": part_numbers,
        })

        if (i + 1) % 25 == 0 or i == len(assemblies) - 1:
            print(f"    ACs: {i+1}/{len(assemblies)}")
        time.sleep(0.25)

    _assert_no_duplicate_acs(meta, slug)
    filepath = os.path.join(META_DIR, f"{slug}_ac_meta.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    total_parts = sum(a["total_parts"] for a in meta)
    print(f"    Saved: {filepath} ({len(meta)} ACs, {total_parts} parts)")
    return filepath


# ── Bihar: oldPdfUrl from API ────────────────────────────────────────────
def build_bihar_meta():
    state_cd = "S04"
    print(f"\n  {state_cd} Bihar (oldPdfUrl)")

    dist_map = get_districts(state_cd)
    time.sleep(0.3)
    assemblies = get_assemblies(state_cd)
    time.sleep(0.3)
    print(f"    Districts: {len(dist_map)}, ACs: {len(assemblies)}")

    meta = []
    for i, ac in enumerate(assemblies):
        ac_no = ac["acNo"]
        ac_name = ac.get("acName", "")
        dist_no = ac.get("distNo", 0)
        dist_name = dist_map.get(dist_no, f"District {dist_no}")

        parts_data = get_parts(state_cd, ac_no)
        parts = []
        for p in parts_data:
            pdf_url = p.get("oldPdfUrl") or ""
            parts.append({
                "part_no": p["partNumber"],
                "pdf_url": pdf_url,
            })
        parts.sort(key=lambda x: x["part_no"])

        meta.append({
            "ac_no": ac_no,
            "ac_name": ac_name,
            "district_name": dist_name,
            "district_no": dist_no,
            "total_parts": len(parts),
            "parts": parts,
        })

        if (i + 1) % 25 == 0 or i == len(assemblies) - 1:
            print(f"    ACs: {i+1}/{len(assemblies)}")
        time.sleep(0.25)

    _assert_no_duplicate_acs(meta, "bihar")
    filepath = os.path.join(META_DIR, "bihar_ac_meta.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    total_parts = sum(a["total_parts"] for a in meta)
    print(f"    Saved: {filepath} ({len(meta)} ACs, {total_parts} parts)")
    return filepath


# ── Gujarat: one ZIP per AC ──────────────────────────────────────────────
def build_gujarat_meta():
    state_cd = "S06"
    ZIP_PATTERN = "https://erms.gujarat.gov.in/ceo-gujarat/ADD_DEL_MOD_LIST/2002/P{ac_no:03d}.zip"
    print(f"\n  {state_cd} Gujarat (ZIP per AC)")

    dist_map = get_districts(state_cd)
    time.sleep(0.3)
    assemblies = get_assemblies(state_cd)
    print(f"    Districts: {len(dist_map)}, ACs: {len(assemblies)}")

    meta = []
    for ac in assemblies:
        ac_no = ac["acNo"]
        ac_name = ac.get("acName", "")
        dist_no = ac.get("distNo", 0)
        dist_name = dist_map.get(dist_no, f"District {dist_no}")

        meta.append({
            "ac_no": ac_no,
            "ac_name": ac_name,
            "district_name": dist_name,
            "district_no": dist_no,
            "zip_url": ZIP_PATTERN.format(ac_no=ac_no),
        })

    _assert_no_duplicate_acs(meta, "gujarat")
    filepath = os.path.join(META_DIR, "gujarat_ac_meta.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    print(f"    Saved: {filepath} ({len(meta)} ACs)")
    return filepath


# ── Chandigarh: single AC, PDFs from CEO site ───────────────────────────
def build_chandigarh_meta():
    state_cd = "U02"
    PDF_PATTERN = "https://ceochandigarh.gov.in//chd/chd{part_no:03d}.PDF"
    print(f"\n  {state_cd} Chandigarh (CEO site PDFs)")

    assemblies = get_assemblies(state_cd)
    time.sleep(0.3)
    ac = assemblies[0] if assemblies else {"acNo": 1, "acName": "CHANDIGARH"}
    parts = get_parts(state_cd, ac["acNo"])
    print(f"    1 AC, {len(parts)} parts")

    part_list = []
    for p in sorted(parts, key=lambda x: x["partNumber"]):
        pno = p["partNumber"]
        part_list.append({
            "part_no": pno,
            "pdf_url": PDF_PATTERN.format(part_no=pno),
        })

    meta = [{
        "ac_no": 1,
        "ac_name": ac.get("acName", "CHANDIGARH"),
        "district_name": "Chandigarh",
        "district_no": 1,
        "total_parts": len(part_list),
        "parts": part_list,
    }]

    _assert_no_duplicate_acs(meta, "chandigarh")
    filepath = os.path.join(META_DIR, "chandigarh_ac_meta.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    print(f"    Saved: {filepath}")
    return filepath


# ── D&NH + Daman & Diu: PDFs from CEO site ──────────────────────────────
def build_dnh_meta():
    state_cd = "U03"
    BASE = "https://ceodaman.nic.in"
    print(f"\n  {state_cd} Dadra & Nagar Haveli and Daman & Diu (CEO site PDFs)")

    dist_map = get_districts(state_cd)
    time.sleep(0.3)
    assemblies = get_assemblies(state_cd)
    time.sleep(0.3)
    print(f"    Districts: {len(dist_map)}, ACs: {len(assemblies)}")

    meta = []
    for ac in assemblies:
        ac_no = ac["acNo"]
        ac_name = ac.get("acName", "")
        dist_no = ac.get("distNo", 0)
        dist_name = dist_map.get(dist_no, f"District {dist_no}")

        parts_data = get_parts(state_cd, ac_no)
        time.sleep(0.3)

        part_list = []
        for p in sorted(parts_data, key=lambda x: x["partNumber"]):
            pno = p["partNumber"]
            if ac_no == 1:
                pdf_url = f"{BASE}/IRER2002/DMN/E{pno}.pdf"
            else:
                pdf_url = f"{BASE}/IRER2002/DNH/Gujarati/PS{pno:02d}-2002.pdf"
            part_list.append({
                "part_no": pno,
                "pdf_url": pdf_url,
            })

        meta.append({
            "ac_no": ac_no,
            "ac_name": ac_name,
            "district_name": dist_name,
            "district_no": dist_no,
            "total_parts": len(part_list),
            "parts": part_list,
        })

    _assert_no_duplicate_acs(meta, "dadra_nagar_haveli_daman_diu")
    filepath = os.path.join(META_DIR, "dadra_nagar_haveli_daman_diu_ac_meta.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    print(f"    Saved: {filepath} ({len(meta)} ACs)")
    return filepath


BUILDERS = {
    "S04": build_bihar_meta,
    "S06": build_gujarat_meta,
    "U02": build_chandigarh_meta,
    "U03": build_dnh_meta,
}


def main():
    os.makedirs(META_DIR, exist_ok=True)

    if len(sys.argv) > 1:
        requested = sys.argv[1:]
    else:
        requested = sorted(set(ECI_STATES.keys()) | set(SPECIAL_STATES.keys()))

    # Validate
    for code in requested:
        if code in SKIP_STATES:
            print(f"SKIP: {code} (already has connector or no data)")
            requested = [c for c in requested if c != code]
        elif code not in ALL_STATES:
            print(f"ERROR: unknown state code {code}")
            sys.exit(1)

    print(f"Will generate meta for {len(requested)} states")
    files = []

    for state_cd in requested:
        if state_cd in BUILDERS:
            f = BUILDERS[state_cd]()
        elif state_cd in ECI_STATES:
            f = build_eci_meta(state_cd)
        else:
            print(f"  SKIP {state_cd}: no builder")
            continue
        files.append(f)

    print(f"\n{'='*60}")
    print(f"DONE. Generated {len(files)} meta files in {META_DIR}")
    for f in files:
        print(f"  {os.path.basename(f)}")


if __name__ == "__main__":
    main()
