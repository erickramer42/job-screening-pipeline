#!/usr/bin/env python3
"""
board_fetch.py — pull public career-board listings for configured
companies, keep metro-area/remote roles whose titles match the
configured patterns, write raw_boards/ in screener quadruple format.

Greenhouse note: base /jobs response carries NO compensation fields;
ranges appear only in ?content=true descriptions inside the
'content-pay-transparency' block. We therefore always fetch content
for Greenhouse and extract the range. Verified live 2026-10-10.
"""
import html as html_mod
import json
import re
import sys
import urllib.request
from pathlib import Path

CONFIG = Path("boards.json")
OUT_DIR = Path("raw_boards")
# Postings that close or stop passing filters move here; the screener
# only globs raw_boards/*.txt, so archived files drop out of ranking
ARCHIVE_DIR = OUT_DIR / "archive"
ANNUALIZE = {"1 YEAR": 1, "1 HOUR": 2080}

API_TEMPLATES = {
    "greenhouse":
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        "?content=true",
    "lever":
        "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby":
        "https://api.ashbyhq.com/posting-api/job-board/{slug}"
        "?includeCompensation=true",
    "smartrecruiters":
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        "?limit=100",
}

# --- top of file, replacing METRO_RE and PAY_FLOOR declarations ---
CONFIG_FILE = Path("pipeline_config.json")

def load_config():
    if not CONFIG_FILE.exists():
        print("[ERROR] pipeline_config.json missing — copy "
              "pipeline_config.example.json and fill in your values.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

CFG = load_config()
PAY_FLOOR = CFG.get("pay_floor", 0)  # base salary floor; cut only
                                          # when range MAX < floor
# Metro city list lives in config (PII routing)
METRO_RE = re.compile("|".join(CFG["metro_cities"]), re.I)

REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b", re.I)
# Only consulted for remote postings without a structured country field.
# remote_regions (config) adds your own region, e.g. a state name and
# its abbreviation, so "Remote - <region>" postings count as well
US_RE = re.compile(
    r"\bus\b|\busa\b|\bu\.s\.|united states|north america"
    + "".join(rf"|\b{re.escape(r)}\b" for r in CFG.get("remote_regions", [])),
    re.I,
)
US_COUNTRIES = {"us", "usa", "united states", "united states of america"}
# A posting is fetched only if its title matches one of these regexes
# (config); an empty list fetches every title
TITLE_RE = re.compile(
    "|".join(f"(?:{p})" for p in CFG["included_title_patterns"]),
    re.I,
)
# Blocked title keywords live in config as plain phrases (not regex).
# Word-bounded so "Internal Tools" / "Sales Platform" engineering roles
# survive; only the non-engineering role itself is blocked
TITLE_BLOCK_RE = re.compile(
    "|".join(rf"\b{re.escape(k)}\b" for k in CFG["excluded_keywords"])
    or r"(?!)",  # empty list blocks nothing
    re.I,
)
# Pay amounts like $85,000 inside the pay-transparency block
PAY_AMT_RE = re.compile(r"\$\s*([0-9][0-9,]{2,})")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)

def fetch_board(platform, slug):
    url = API_TEMPLATES[platform].format(slug=slug)
    if platform != "smartrecruiters":
        return get_json(url)
    # SmartRecruiters caps pages at 100; follow offset to totalFound
    postings = []
    while True:
        page = get_json(f"{url}&offset={len(postings)}")
        content = page.get("content") or []
        postings.extend(content)
        if not content or len(postings) >= page.get("totalFound", 0):
            return {"content": postings}

def location_ok(location, remote, country):
    """Postings in a configured metro city, or remote postings open
    to the US.

    remote/country come from structured API fields where the platform
    has them (None otherwise); the location text is the fallback.
    """
    if METRO_RE.search(location):
        return True
    if not (remote or REMOTE_RE.search(location)):
        return False
    if country:
        return country.strip().lower() in US_COUNTRIES
    # No country field: keep bare "Remote" or US-marked text, drop
    # "Remote - Canada", "Remote (EMEA)", ...
    rest = REMOTE_RE.sub("", location)
    return bool(US_RE.search(location)) or not re.search(r"[a-z]", rest, re.I)

def extract_greenhouse_pay(content_html):
    """Parse base salary range from a Greenhouse description.

    Returns (min, max) in dollars, or (None, None) when the posting
    has no pay-transparency block. Unescapes entities first since the
    API delivers HTML-encoded content.
    """
    if not content_html:
        return (None, None)
    text = html_mod.unescape(content_html)
    marker = "content-pay-transparency"
    idx = text.find(marker)
    if idx == -1:
        return (None, None)
    # Look only inside the transparency block (bounded window; the
    # conclusion div after it sometimes mentions unrelated figures)
    window = text[idx:idx + 4000]
    amounts = [int(m.replace(",", "")) for m in PAY_AMT_RE.findall(window)]
    if not amounts:
        return (None, None)
    lo, hi = min(amounts), max(amounts)
    # Sanity: reject absurd ranges (annualized dollars expected)
    if lo < 10_000 or hi > 1_000_000:
        return (None, None)
    return (lo, hi)

def flatten(board, platform, slug):
    """Normalize each platform's schema to (id, title, location, url,
    pay_str, pay_min, pay_max, remote, country). remote/country are None
    when the platform doesn't expose them."""
    jobs = []
    if platform == "greenhouse":
        for j in board.get("jobs", []):
            lo, hi = extract_greenhouse_pay(j.get("content"))
            pay = f"${lo:,}-${hi:,}" if lo is not None else ""
            jobs.append((str(j.get("id")), j.get("title", ""),
                         (j.get("location") or {}).get("name", "Unknown"),
                         j.get("absolute_url", ""), pay, lo, hi,
                         None, None))
    elif platform == "lever":
        for j in board:
            cats = j.get("categories") or {}
            locs = (cats.get("allLocations")
                    or [cats.get("location") or "Unknown"])
            jobs.append((j.get("id", ""), j.get("text", ""),
                         "; ".join(locs),
                         j.get("hostedUrl", ""), "", None, None,
                         j.get("workplaceType") == "remote",
                         j.get("country")))
    elif platform == "ashby":
        for j in board.get("jobs", []):
            pay = ""
            lo = hi = None
            # Verified schema (2026-10-10, live board):
            # compensation.summaryComponents[] ->
            #   compensationType ("Salary"|"EquityPercentage"|...),
            #   minValue/maxValue, currencyCode, interval
            # Only USD is comparable to PAY_FLOOR (boards mix EUR/GBP/
            # CAD/JPY); other currencies/intervals stay "pay unknown"
            comps = ((j.get("compensation") or {})
                     .get("summaryComponents") or [])
            salary_tiers = [c for c in comps
                            if c.get("compensationType") == "Salary"
                            and c.get("currencyCode") == "USD"
                            and c.get("interval") in ANNUALIZE
                            and isinstance(c.get("minValue"), (int, float))]
            if salary_tiers:
                lo = int(min(c["minValue"] * ANNUALIZE[c["interval"]]
                             for c in salary_tiers))
                hi = int(max((c.get("maxValue") or c["minValue"])
                             * ANNUALIZE[c["interval"]]
                             for c in salary_tiers))
                pay = f"${lo:,}-${hi:,}"
            locs = [j.get("location") or "Unknown"] + [
                s["location"] for s in j.get("secondaryLocations") or []
                if s.get("location")]
            postal = ((j.get("address") or {}).get("postalAddress") or {})
            jobs.append((j.get("id", ""), j.get("title", ""),
                         "; ".join(locs),
                         j.get("jobUrl") or "", pay, lo, hi,
                         bool(j.get("isRemote"))
                         or j.get("workplaceType") == "Remote",
                         postal.get("addressCountry")))
    elif platform == "smartrecruiters":
        for j in board.get("content", []):
            loc = j.get("location") or {}
            jid = j.get("id", "")
            # 'ref' is the API URL; this is the human-facing posting page
            jobs.append((jid, j.get("name", ""),
                         loc.get("fullLocation") or loc.get("city")
                         or "Unknown",
                         f"https://jobs.smartrecruiters.com/{slug}/{jid}",
                         "", None, None,
                         loc.get("remote"), loc.get("country")))
    return jobs

def archive_stale(platform, slug, live_names):
    """Move this board's files that are no longer live (posting closed,
    or no longer passing filters) into ARCHIVE_DIR."""
    source = f"Source: {platform}/{slug}"
    moved = 0
    for f in OUT_DIR.glob(f"{platform}_*.txt"):
        if f.name in live_names:
            continue
        head = f.read_text(encoding="utf-8",
                           errors="replace").splitlines()[:3]
        if source not in head:   # another board on the same platform
            continue
        ARCHIVE_DIR.mkdir(exist_ok=True)
        f.replace(ARCHIVE_DIR / f.name)
        moved += 1
    return moved

def main():
    if not CONFIG.exists():
        print("No boards.json found — copy boards.example.json and "
              "add your boards.")
        sys.exit(1)
    OUT_DIR.mkdir(exist_ok=True)

    boards = json.loads(CONFIG.read_text(encoding="utf-8"))
    new_jobs = 0
    for board_cfg in boards:
        platform, slug = board_cfg["platform"], board_cfg["slug"]
        company = board_cfg.get("company", slug)
        try:
            data = fetch_board(platform, slug)
        except Exception as e:
            print(f"  {company}: FETCH FAILED ({e}) — check slug/platform")
            continue

        kept = cut_pay = 0
        live = set()
        for (jid, title, location, jurl, pay, lo, hi, remote,
             country) in flatten(data, platform, slug):
            location = location or ""
            if not TITLE_RE.search(title) or TITLE_BLOCK_RE.search(title):
                continue
            if not location_ok(location, remote, country):
                continue
            # Make structured remote flags visible to the LLM, which
            # sees only title/employer/location
            if remote and not REMOTE_RE.search(location):
                location = f"{location} (Remote)"
            # Deterministic pay floor: cut only when the RANGE MAX is
            # below floor (with a $100k floor, a $90k-$110k range clears
            # the bar; unknown pay stays in and defers to LLM judgment)
            if lo is not None and hi is not None and hi < PAY_FLOOR:
                cut_pay += 1
                continue
            fname = OUT_DIR / f"{platform}_{jid}.txt"
            live.add(fname.name)
            if fname.exists():
                continue
            loc_line = f"{location} ({pay})" if pay else location
            with open(fname, "w", encoding="utf-8") as f:
                f.write("# Metadata\n")
                f.write(f"Source: {platform}/{slug}\n")
                f.write(f"Company: {company}\n")
                f.write(f"URL: {jurl}\n")
                f.write(f"Title: {title}\n")
                if pay:
                    f.write(f"Pay: {pay}\n")
                f.write("---\n\n")
                f.write(f"{title}\n{company}\n{loc_line}\nView\n")
            new_jobs += 1
            kept += 1
        archived = archive_stale(platform, slug, live)
        print(f"  {company}: {kept} new, {len(live)} live, "
              f"{cut_pay} cut by pay floor, {archived} archived")

    print(f"\nBoards: wrote {new_jobs} new postings to {OUT_DIR}/")

if __name__ == "__main__":
    main()
    