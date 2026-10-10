#!/usr/bin/env python3
"""
slug_probe.py — guess-and-verify board slugs for a list of company
names against the four public job-board APIs. Prints verified entries
ready to paste into boards.json.

Input: candidates.txt — one company name per line (or pulled from
out/ranked_*.json automatically when no candidates file is given).
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

PROBES = {
    "greenhouse":
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever":
        "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby":
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters":
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
}

# Common slug mutations beyond the plain name: many orgs append
# or strip words like "inc", "labs", "technologies"
SUFFIXES = ["", "-inc", "inc", "-technologies", "technologies",
            "-labs", "labs", "-hq"]

def slug_variants(name):
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    base_dash = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    variants = {base, base_dash}
    # Drop trailing corporate words before suffixing
    stem = re.sub(
        r"(inc|llc|corp|corporation|co|company|technologies|systems)$",
        "", base).strip("-")
    if stem and stem != base:
        variants.add(stem)
    for s in SUFFIXES:
        variants.add(stem + s)
    return sorted(variants)

def probe(slug, platform):
    """True if the endpoint returns live JSON with job data."""
    url = PROBES[platform].format(slug=slug)
    req = urllib.request.Request(
        url, headers={"User-Agent": "job-pipeline/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return False
    if platform == "greenhouse":
        return bool(data.get("jobs"))
    if platform == "lever":
        return isinstance(data, list) and len(data) > 0
    if platform == "ashby":
        return bool(data.get("jobs"))
    if platform == "smartrecruiters":
        return bool(data.get("content"))
    return False

def candidates():
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        return [ln.strip() for ln in
                Path(sys.argv[1]).read_text().splitlines() if ln.strip()]
    # Auto mode: top employers from the latest ranked_*.json
    out = sorted(Path("out").glob("ranked_*.json"))
    if not out:
        print("No candidates file and no ranked_*.json — pass a file.")
        sys.exit(1)
    ranked = json.load(open(out[-1]))["ranked"]
    names = [j["employer"] for j in ranked[:200]
             if j.get("score", 0) >= 60]
    # Dedupe, preserve order, drop staffing noise and unknowns
    seen, result = set(), []
    for n in names:
        key = n.lower()
        if key not in seen and "not available" not in key \
                and not any(f in key for f in
                           ("teksystems", "insight global", "robert half",
                            "manpower", "ksn")):
            seen.add(key)
            result.append(n)
    print(f"Auto-derived {len(result)} candidate employers "
          f"from {out[-1].name} (score >= 60)")
    return result

def main():
    hits = 0
    for name in candidates():
        found = []
        for variant in slug_variants(name):
            for platform in PROBES:
                if probe(variant, platform):
                    found.append((platform, variant))
                    break  # first platform hit is enough
            if found:
                break
        if found:
            platform, slug = found[0]
            print(f'  {{"platform": "{platform}", "slug": "{slug}", '
                  f'"company": "{name}"}},   <- VERIFIED')
            hits += 1
        else:
            print(f"  # {name}: no board found on any platform")
    print(f"\n{hits} verifiable boards found — paste the VERIFIED "
          f"lines above into boards.json")

if __name__ == "__main__":
    main()
    