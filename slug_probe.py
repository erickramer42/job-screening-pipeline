#!/usr/bin/env python3
"""
slug_probe.py — guess-and-verify board slugs for a list of company
names against the four public job-board APIs. Prints verified entries
ready to paste into boards.json.

Input: a file with one company name per line, e.g. targets.txt (or,
when no file is given, employers pulled from the latest
out/ranked_<date>.json).
"""
import json
import re
import sys
import urllib.error
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
    """Candidate slugs, most likely first: the plain name, then the name
    without a trailing corporate word, then common suffixes. main() stops
    at the first hit, so order matters."""
    base = re.sub(r"[^a-z0-9]", "", name.lower())
    base_dash = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # Drop a trailing corporate WORD before suffixing. Matched on the
    # dashed form, so "Cisco" and "Costco" keep their "co"
    stem_dash = re.sub(
        r"-(inc|llc|corp|corporation|co|company|technologies|systems)$",
        "", base_dash)
    stem = stem_dash.replace("-", "")
    variants = [base, base_dash, stem, stem_dash]
    variants += [stem + s for s in SUFFIXES]
    return list(dict.fromkeys(v for v in variants if v))

def probe(slug, platform):
    """True if the endpoint returns live JSON with job data; False when
    the platform has no such board (a 4xx answer) or it has no jobs.
    Raises OSError when the platform can't be asked (network down,
    timeout, a 5xx answer), so an outage isn't reported as "no board"."""
    url = PROBES[platform].format(slug=slug)
    req = urllib.request.Request(
        url, headers={"User-Agent": "job-pipeline/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return False
        raise
    except ValueError:  # not JSON: not a posting API answer
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
    if len(sys.argv) > 1:
        # A mistyped name stops here rather than falling through to auto
        # mode, which would probe a different list over the network
        path = Path(sys.argv[1])
        if not path.is_file():
            print(f"[ERROR] {path} not found.")
            sys.exit(1)
        return [ln.strip() for ln in
                path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    # Auto mode: top employers from the latest ranked_<date>.json. The
    # dated pattern skips other files in out/ (a "ranked_v1_..." name
    # would sort after every date and be picked as the latest)
    out = sorted(Path("out").glob("ranked_????-??-??.json"))
    if not out:
        print("No candidates file and no ranked_<date>.json — pass a file.")
        sys.exit(1)
    ranked = json.loads(out[-1].read_text(encoding="utf-8"))["ranked"]
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
    hits = failed = 0
    for name in candidates():
        found = []
        try:
            for variant in slug_variants(name):
                for platform in PROBES:
                    if probe(variant, platform):
                        found.append((platform, variant))
                        break  # first platform hit is enough
                if found:
                    break
        except OSError as e:
            print(f"  # {name}: lookup failed ({e}); try again later")
            failed += 1
            continue
        if found:
            platform, slug = found[0]
            # json.dumps, so a name with a quote still pastes as JSON
            entry = json.dumps({"platform": platform, "slug": slug,
                                "company": name}, ensure_ascii=False)
            print(f"  {entry},   <- VERIFIED")
            hits += 1
        else:
            print(f"  # {name}: no board found on any platform")
    print(f"\n{hits} verifiable boards found — paste the VERIFIED "
          f"lines above into boards.json")
    if failed:
        print(f"[ERROR] {failed} lookup(s) failed; those companies are "
              f"unchecked, not boardless.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
    