#!/usr/bin/env python3
"""
board_fetch.py — pull public career-board listings for configured
companies, keep metro-area/remote roles whose titles match the
configured patterns, write raw_boards/ in screener quadruple format.

Greenhouse note: base /jobs response carries NO compensation fields;
ranges appear only in ?content=true descriptions inside the
'content-pay-transparency' block. We therefore always fetch content
for Greenhouse and extract the range. Verified live 2026-10-10.

Descriptions: stored below a '--- DESCRIPTION ---' marker after the
'View' line. The screener stops quadruple parsing at that marker, so
description prose can never be misread as a listing. Text conversion
(html_text.py) runs for every posting that passes the filters.
  - Greenhouse: 'content' is HTML-escaped HTML: decoded once to get
    the markup, then html_to_text
  - Ashby: 'descriptionPlain'  (verified live 2026-10-10)
  - SmartRecruiters: not in the list response (verified 2026-10-10);
    fetched from the detail endpoint jobAd.sections.{jobDescription,
    qualifications}, one request per posting, so only when the
    posting's listing lines changed or its file has no description
  - Lever: no boards in use; captures "" until the day one is added
    and its schema is probed

Each posting's file is rewritten whenever its text would change: the
employer edited the posting, FILE_FORMAT was bumped (files carry a
'Format:' line), or a SmartRecruiters detail fetch failed last time.
"""
import html as html_mod
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from html_text import html_to_text, normalize_text
from location_filter import REMOTE_RE, location_matcher

CONFIG = Path("boards.json")
OUT_DIR = Path("raw_boards")
# Postings that close or stop passing filters move here; the screener
# only globs raw_boards/*.txt, so archived files drop out of ranking
ARCHIVE_DIR = OUT_DIR / "archive"
ANNUALIZE = {"1 YEAR": 1, "1 HOUR": 2080}
# Same marker jobs_screen.py stops quadruple parsing at
DESC_MARKER = "--- DESCRIPTION ---"
# Bump when the file layout or description text changes. The Format
# line is part of every file, so each one is then rewritten on the next
# run, SmartRecruiters descriptions included. 2: line-preserving text,
# entities decoded after tag stripping
FILE_FORMAT = 2

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
SMARTRECRUITERS_DETAIL = ("https://api.smartrecruiters.com/v1/"
                          "companies/{slug}/postings/{jid}")
# Ad sections worth keeping: role duties and requirements. companyDescription
# is employer boilerplate and adds no per-role signal; additionalInformation
# is mostly EEO/legal filler.
SMARTRECRUITERS_SECTION_KEYS = ("jobDescription", "qualifications")

CONFIG_FILE = Path("pipeline_config.json")

def load_config():
    if not CONFIG_FILE.exists():
        print("[ERROR] pipeline_config.json missing — copy "
              "pipeline_config.example.json and fill in your values.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

CFG = load_config()
# Annual base salary floor: a posting is cut only when the top of its
# published range is below it
PAY_FLOOR = CFG.get("pay_floor", 0)
# Postings in a configured metro city, or remote postings open to the
# US (the screener applies the same rule to digest rows)
location_ok = location_matcher(CFG["metro_cities"],
                               CFG.get("remote_regions", []))
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

def fetch_smartrecruiters_detail(slug, jid):
    """One posting's detail payload; carries the job ad sections the
    list endpoint omits. Raises on network/API failure — caller decides
    whether that's fatal (it never is: posting goes out description-less)."""
    return get_json(SMARTRECRUITERS_DETAIL.format(slug=slug, jid=jid))

def extract_smartrecruiters_description(detail):
    """Build the description text from a posting-detail payload.

    Sections sit at jobAd.sections.<key>. The per-section shape is
    handled defensively: str, {'title','text'}, or list of text blocks
    all work, and anything else contributes nothing. Schema drift
    therefore yields a shorter description, not a crash.
    """
    sections = ((detail or {}).get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in SMARTRECRUITERS_SECTION_KEYS:
        sec = sections.get(key)
        if sec is None:
            continue
        title, text = key, ""
        if isinstance(sec, dict):
            title = sec.get("title") or key
            text = sec.get("text") or ""
        elif isinstance(sec, str):
            text = sec
        elif isinstance(sec, list):
            chunks = []
            for blk in sec:
                if isinstance(blk, dict):
                    chunks.append(str(blk.get("text")
                                       or blk.get("content") or ""))
                elif isinstance(blk, str):
                    chunks.append(blk)
            text = "\n".join(c for c in chunks if c)
        text = html_to_text(text)
        if text:
            parts.append(f"{title}\n{text}")
    return "\n\n".join(parts)

def description_text(platform, raw):
    """Plain-text description from the raw value flatten() captured."""
    if not raw:
        return ""
    if platform == "greenhouse":
        # HTML-escaped HTML: decode once to get the markup; html_to_text
        # then strips tags and decodes the entities inside it
        return html_to_text(html_mod.unescape(raw))
    return normalize_text(raw)

def record_text(platform, slug, company, url, title, pay, loc_line, desc):
    """A posting file's full text: metadata, the title / company /
    location / View quadruple, then the description, if any."""
    lines = ["# Metadata", f"Source: {platform}/{slug}",
             f"Company: {company}", f"URL: {url}", f"Title: {title}"]
    if pay:
        lines.append(f"Pay: {pay}")
    lines += [f"Format: {FILE_FORMAT}", "---", "",
              title, company, loc_line, "View"]
    text = "\n".join(lines) + "\n"
    # Stored in full for audit/replay; truncation happens only at
    # prompt time (pass 2), never on disk
    if desc:
        text += f"\n{DESC_MARKER}\n{desc}\n"
    return text

def write_atomic(path, text):
    """Write through a temporary file, so an interrupted run leaves the
    old file or the new one, never a truncated one. The .tmp name
    matches no glob the pipeline reads."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

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
    pay_str, pay_min, pay_max, remote, country, raw_description).
    remote/country are None when the platform doesn't expose them.
    raw_description is unconverted (see description_text) or "";
    sync_board() converts it only for postings that pass the filters.
    SmartRecruiters descriptions are fetched there too, only when they
    may have changed (see sync_board)."""
    jobs = []
    if platform == "greenhouse":
        for j in board.get("jobs", []):
            lo, hi = extract_greenhouse_pay(j.get("content"))
            pay = f"${lo:,}-${hi:,}" if lo is not None else ""
            jobs.append((str(j.get("id")), j.get("title", ""),
                         (j.get("location") or {}).get("name", "Unknown"),
                         j.get("absolute_url", ""), pay, lo, hi,
                         None, None,
                         j.get("content") or ""))
    elif platform == "lever":
        for j in board:
            cats = j.get("categories") or {}
            locs = (cats.get("allLocations")
                    or [cats.get("location") or "Unknown"])
            jobs.append((j.get("id", ""), j.get("text", ""),
                         "; ".join(locs),
                         j.get("hostedUrl", ""), "", None, None,
                         j.get("workplaceType") == "remote",
                         j.get("country"),
                         ""))  # description: schema not probed yet
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
                         postal.get("addressCountry"),
                         j.get("descriptionPlain") or ""))
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
                         loc.get("remote"), loc.get("country"),
                         ""))  # filled in main() after filters pass
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

def sync_board(board_cfg):
    """Fetch one boards.json entry, write its passing postings, and
    archive its files that no longer pass. Returns the number of new
    postings. Raises on a malformed entry or any fetch/schema failure;
    the caller skips the board, so nothing of it is archived on
    incomplete information."""
    if not isinstance(board_cfg, dict):
        raise ValueError("entry is not a JSON object")
    platform, slug = board_cfg.get("platform"), board_cfg.get("slug")
    if platform not in API_TEMPLATES or not slug:
        raise ValueError("entry needs a slug and a platform of "
                         + ", ".join(API_TEMPLATES))
    company = board_cfg.get("company", slug)
    data = fetch_board(platform, slug)

    new = rewritten = cut_pay = 0
    live = set()
    for (jid, title, location, jurl, pay, lo, hi, remote,
         country, raw_desc) in flatten(data, platform, slug):
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
        old = (fname.read_text(encoding="utf-8", errors="replace")
               if fname.exists() else None)
        loc_line = f"{location} ({pay})" if pay else location
        if platform == "smartrecruiters":
            # The description costs one detail request per posting, so
            # the stored one is kept while the listing lines are
            # unchanged. A file without one (its fetch failed) retries.
            # A failed fetch costs the text, not the posting: it still
            # writes and ranks.
            head = record_text(platform, slug, company, jurl, title, pay,
                               loc_line, "")
            old_head, _, old_desc = (old or "").partition(
                f"\n{DESC_MARKER}\n")
            desc = old_desc.removesuffix("\n") if old_head == head else ""
            if not desc and jid:
                try:
                    desc = extract_smartrecruiters_description(
                        fetch_smartrecruiters_detail(slug, jid))
                except Exception as e:
                    print(f"    {company}: detail fetch failed for "
                          f"{jid} ({e}) — written without description")
        else:
            desc = description_text(platform, raw_desc)
        text = record_text(platform, slug, company, jurl, title, pay,
                           loc_line, desc)
        if text == old:
            continue
        write_atomic(fname, text)
        if old is None:
            new += 1
        else:
            rewritten += 1
    archived = archive_stale(platform, slug, live)
    print(f"  {company}: {new} new, {rewritten} rewritten, "
          f"{len(live)} live, {cut_pay} cut by pay floor, "
          f"{archived} archived")
    return new

def main():
    if not CONFIG.exists():
        print("No boards.json found — copy boards.example.json and "
              "add your boards.")
        return 1
    OUT_DIR.mkdir(exist_ok=True)

    boards = json.loads(CONFIG.read_text(encoding="utf-8"))
    new_jobs = failed = 0
    # One bad entry or board never stops the others; any failure makes
    # the exit code non-zero so the scheduled run logs it
    for board_cfg in boards:
        try:
            new_jobs += sync_board(board_cfg)
        except Exception as e:
            name = ((board_cfg.get("company") or board_cfg.get("slug"))
                    if isinstance(board_cfg, dict) else None)
            print(f"  {name or repr(board_cfg)}: FAILED ({e}) — check "
                  f"slug/platform")
            failed += 1

    print(f"\nBoards: wrote {new_jobs} new postings to {OUT_DIR}/")
    if failed:
        print(f"[ERROR] {failed} of {len(boards)} board(s) failed.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
    