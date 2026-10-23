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
  - Lever: 'description', each of 'lists' (a heading and <li> items),
    then 'additional', all HTML (verified live 2026-10-23)

Greenhouse location: some boards put only the work arrangement in
location.name ("Hybrid", "In-Office", "Distributed") and the city in
the posting's offices. For such a name the offices are appended:
"Hybrid: Springfield, ST". Every other name is used as it is.

Each posting's file is rewritten whenever its text would change: the
employer edited the posting, FILE_FORMAT was bumped (files carry a
'Format:' line), or a SmartRecruiters detail fetch failed last time.
Files move to raw_boards/archive/ when their posting closes or stops
passing the filters, or when their board leaves boards.json.
"""
import html as html_mod
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from html_text import html_to_text, normalize_text
from location_filter import REMOTE_RE, location_matcher
from pipeline_common import DESC_MARKER, configure_from_file, write_atomic

CONFIG = Path("boards.json")
OUT_DIR = Path("raw_boards")
# Postings that close or stop passing filters move here; the screener
# only globs raw_boards/*.txt, so archived files drop out of ranking
ARCHIVE_DIR = OUT_DIR / "archive"
ANNUALIZE = {"1 YEAR": 1, "1 HOUR": 2080}
LEVER_ANNUALIZE = {"per-year-salary": 1, "per-hour-wage": 2080}
# Bump when the file layout or description text changes. The Format
# line is part of every file, so each one is then rewritten on the next
# run, SmartRecruiters descriptions included. 2: line-preserving text,
# entities decoded after tag stripping. 3: a Posted: line; no Title:
# or Pay: line, which nothing read
FILE_FORMAT = 3
# A Greenhouse location.name that states only how the job is worked.
# Not "remote": a bare "Remote" already counts as US remote
PLACELESS_RE = re.compile(
    r"(?:hybrid|in[- ]?office|on[- ]?site|distributed|flexible)"
    r"(?:[\s;,/]+(?:hybrid|in[- ]?office|on[- ]?site|distributed"
    r"|flexible))*", re.I)

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

def configure(cfg):
    """Set the filters from a config dict. main() passes
    pipeline_config.json and tests pass their own, so importing the
    module reads no files. A missing required key raises KeyError, and
    a bad title pattern re.error."""
    global PAY_FLOOR, location_ok, TITLE_RE, TITLE_BLOCK_RE
    # Annual base salary floor: a posting is cut only when the top of
    # its published range is below it
    PAY_FLOOR = cfg.get("pay_floor", 0)
    # Postings in a configured metro city, or remote postings open to
    # the US (the screener applies the same rule to digest rows)
    location_ok = location_matcher(cfg["metro_cities"],
                                   cfg.get("remote_regions", []))
    # A posting is fetched only if its title matches one of these
    # regexes (config); an empty list fetches every title
    TITLE_RE = re.compile(
        "|".join(f"(?:{p})" for p in cfg["included_title_patterns"]),
        re.I,
    )
    # Blocked title keywords live in config as plain phrases (not
    # regex). Word-bounded so "Internal Tools" / "Sales Platform"
    # engineering roles survive; only the non-engineering role itself
    # is blocked
    TITLE_BLOCK_RE = re.compile(
        "|".join(rf"\b{re.escape(k)}\b" for k in cfg["excluded_keywords"])
        or r"(?!)",  # empty list blocks nothing
        re.I,
    )

# Until main() configures it, the module runs as for an empty config
configure({"metro_cities": [], "included_title_patterns": [],
           "excluded_keywords": []})

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
    if platform == "lever":
        return html_to_text(raw)
    return normalize_text(raw)

def record_text(platform, slug, company, url, posted, title, loc_line,
                desc):
    """A posting file's full text: metadata, the title / company /
    location / View quadruple, then the description, if any."""
    lines = ["# Metadata", f"Source: {platform}/{slug}",
             f"Company: {company}", f"URL: {url}"]
    if posted:
        lines.append(f"Posted: {posted}")
    lines += [f"Format: {FILE_FORMAT}", "---", "",
              title, company, loc_line, "View"]
    text = "\n".join(lines) + "\n"
    # Stored in full for audit/replay; truncation happens only at
    # prompt time (pass 2), never on disk
    if desc:
        text += f"\n{DESC_MARKER}\n{desc}\n"
    return text

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

class Posting(NamedTuple):
    """One posting, as flatten() normalizes it from any platform."""
    id: str
    title: str
    location: str
    url: str
    pay: str                # "$lo-$hi", or "" when unknown
    pay_min: int | None
    pay_max: int | None
    remote: bool | None     # None when the platform doesn't say
    country: str | None
    # Unconverted (see description_text), or "". sync_board() converts
    # it only for postings that pass the filters
    raw_description: str = ""
    posted: str = ""        # YYYY-MM-DD, or "" when unknown

def posted_date(value):
    """YYYY-MM-DD from an ISO timestamp, or from epoch milliseconds
    (Lever); "" for anything else. The date alone, so a re-stamped time
    of day doesn't rewrite the file."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000,
                                          timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return ""

def greenhouse_location(name, offices):
    """(location, remote) for a Greenhouse posting. A name that states
    only the work arrangement gets its offices' locations appended, and
    "Distributed" counts as remote. Any other name is kept as it is,
    with remote unknown."""
    if not (isinstance(name, str) and PLACELESS_RE.fullmatch(name.strip())):
        return name, None
    places = dict.fromkeys(
        (o.get("location") or o.get("name") or "").strip()
        for o in offices or [] if isinstance(o, dict))
    places.pop("", None)
    remote = True if re.search("distributed", name, re.I) else None
    if places:
        name = f"{name}: {'; '.join(places)}"
    return name, remote

def flatten(board, platform, slug):
    """Normalize each platform's schema to a list of Posting.
    SmartRecruiters postings come without a description: sync_board()
    fetches it, only when it may have changed."""
    jobs = []
    if platform == "greenhouse":
        for j in board.get("jobs", []):
            lo, hi = extract_greenhouse_pay(j.get("content"))
            location, remote = greenhouse_location(
                (j.get("location") or {}).get("name", "Unknown"),
                j.get("offices"))
            jobs.append(Posting(
                id=str(j.get("id")), title=j.get("title", ""),
                location=location, url=j.get("absolute_url", ""),
                pay=f"${lo:,}-${hi:,}" if lo is not None else "",
                pay_min=lo, pay_max=hi, remote=remote, country=None,
                raw_description=j.get("content") or "",
                posted=posted_date(j.get("first_published"))))
    elif platform == "lever":
        for j in board:
            cats = j.get("categories") or {}
            locs = (cats.get("allLocations")
                    or [cats.get("location") or "Unknown"])
            # Verified schema (2026-10-23, live boards): salaryRange ->
            # min, max, currency, interval ("per-year-salary",
            # "per-hour-wage", ...). Only USD per year or hour compares
            # to PAY_FLOOR
            sal = j.get("salaryRange") or {}
            pay = ""
            lo = hi = None
            if (sal.get("currency") == "USD"
                    and sal.get("interval") in LEVER_ANNUALIZE
                    and isinstance(sal.get("min"), (int, float))):
                k = LEVER_ANNUALIZE[sal["interval"]]
                top = sal.get("max")
                lo = int(sal["min"] * k)
                hi = int((top if isinstance(top, (int, float))
                          else sal["min"]) * k)
                pay = f"${lo:,}-${hi:,}"
            # The body, then each list under its heading, then the
            # closing text; <br> keeps the parts on separate lines
            parts = [j.get("description") or ""]
            parts += [f"<h3>{html_mod.escape(lst.get('text') or '')}</h3>"
                      f"<ul>{lst.get('content') or ''}</ul>"
                      for lst in j.get("lists") or []
                      if isinstance(lst, dict)]
            parts.append(j.get("additional") or "")
            jobs.append(Posting(
                id=j.get("id", ""), title=j.get("text", ""),
                location="; ".join(locs), url=j.get("hostedUrl", ""),
                pay=pay, pay_min=lo, pay_max=hi,
                remote=j.get("workplaceType") == "remote",
                country=j.get("country"),
                raw_description="<br>".join(p for p in parts if p),
                posted=posted_date(j.get("createdAt"))))
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
            jobs.append(Posting(
                id=j.get("id", ""), title=j.get("title", ""),
                location="; ".join(locs), url=j.get("jobUrl") or "",
                pay=pay, pay_min=lo, pay_max=hi,
                remote=(bool(j.get("isRemote"))
                        or j.get("workplaceType") == "Remote"),
                country=postal.get("addressCountry"),
                raw_description=j.get("descriptionPlain") or "",
                posted=posted_date(j.get("publishedAt"))))
    elif platform == "smartrecruiters":
        for j in board.get("content", []):
            loc = j.get("location") or {}
            jid = j.get("id", "")
            # 'ref' is the API URL; this is the human-facing posting page
            jobs.append(Posting(
                id=jid, title=j.get("name", ""),
                location=(loc.get("fullLocation") or loc.get("city")
                          or "Unknown"),
                url=f"https://jobs.smartrecruiters.com/{slug}/{jid}",
                pay="", pay_min=None, pay_max=None,
                remote=loc.get("remote"), country=loc.get("country"),
                posted=posted_date(j.get("releasedDate"))))
    return jobs

def board_source(board_cfg):
    """The "platform/slug" a boards.json entry names, as its files'
    Source: line states it. Raises ValueError for a malformed entry."""
    if not isinstance(board_cfg, dict):
        raise ValueError("entry is not a JSON object")
    platform, slug = board_cfg.get("platform"), board_cfg.get("slug")
    if platform not in API_TEMPLATES or not slug:
        raise ValueError("entry needs a slug and a platform of "
                         + ", ".join(API_TEMPLATES))
    return f"{platform}/{slug}"

def file_source(f):
    """The "platform/slug" from a posting file's Source: line, or None."""
    head = f.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
    return next((ln[len("Source: "):] for ln in head
                 if ln.startswith("Source: ")), None)

def archive(f):
    ARCHIVE_DIR.mkdir(exist_ok=True)
    f.replace(ARCHIVE_DIR / f.name)

def archive_stale(platform, slug, live_names):
    """Move this board's files that are no longer live (posting closed,
    or no longer passing filters) into ARCHIVE_DIR."""
    moved = 0
    for f in OUT_DIR.glob(f"{platform}_*.txt"):
        if f.name in live_names:
            continue
        # Another board on the same platform shares the file prefix
        if file_source(f) != f"{platform}/{slug}":
            continue
        archive(f)
        moved += 1
    return moved

def archive_unconfigured(sources):
    """Move the files of boards not in boards.json (removed, or given a
    new slug) into ARCHIVE_DIR; archive_stale never visits them, so
    they'd otherwise be screened every day with frozen text. A file
    without a Source: line is left alone. Returns {source: files moved}."""
    moved = {}
    for f in OUT_DIR.glob("*.txt"):
        source = file_source(f)
        if source is None or source in sources:
            continue
        archive(f)
        moved[source] = moved.get(source, 0) + 1
    return moved

def sync_board(board_cfg):
    """Fetch one boards.json entry, write its passing postings, and
    archive its files that no longer pass. Returns the number of new
    postings. Raises on a malformed entry or any fetch/schema failure;
    the caller skips the board, so nothing of it is archived on
    incomplete information."""
    board_source(board_cfg)  # a malformed entry raises here
    platform, slug = board_cfg["platform"], board_cfg["slug"]
    company = board_cfg.get("company", slug)
    data = fetch_board(platform, slug)

    new = rewritten = cut_pay = 0
    live = set()
    postings = flatten(data, platform, slug)
    for p in postings:
        location = p.location or ""
        if not TITLE_RE.search(p.title) or TITLE_BLOCK_RE.search(p.title):
            continue
        if not location_ok(location, p.remote, p.country):
            continue
        # Make structured remote flags visible to the LLM, which
        # sees only title/employer/location
        if p.remote and not REMOTE_RE.search(location):
            location = f"{location} (Remote)"
        # Deterministic pay floor: cut only when the RANGE MAX is
        # below floor (with a $100k floor, a $90k-$110k range clears
        # the bar; unknown pay stays in and defers to LLM judgment)
        if p.pay_min is not None and p.pay_max is not None \
                and p.pay_max < PAY_FLOOR:
            cut_pay += 1
            continue
        fname = OUT_DIR / f"{platform}_{p.id}.txt"
        live.add(fname.name)
        old = (fname.read_text(encoding="utf-8", errors="replace")
               if fname.exists() else None)
        loc_line = f"{location} ({p.pay})" if p.pay else location
        if platform == "smartrecruiters":
            # The description costs one detail request per posting, so
            # the stored one is kept while the listing lines are
            # unchanged. A file without one (its fetch failed) retries.
            # A failed fetch costs the text, not the posting: it still
            # writes and ranks.
            head = record_text(platform, slug, company, p.url, p.posted,
                               p.title, loc_line, "")
            old_head, _, old_desc = (old or "").partition(
                f"\n{DESC_MARKER}\n")
            desc = old_desc.removesuffix("\n") if old_head == head else ""
            if not desc and p.id:
                try:
                    desc = extract_smartrecruiters_description(
                        fetch_smartrecruiters_detail(slug, p.id))
                except Exception as e:
                    print(f"    {company}: detail fetch failed for "
                          f"{p.id} ({e}) — written without description")
        else:
            desc = description_text(platform, p.raw_description)
        text = record_text(platform, slug, company, p.url, p.posted,
                           p.title, loc_line, desc)
        if text == old:
            continue
        write_atomic(fname, text)
        if old is None:
            new += 1
        else:
            rewritten += 1
    archived = archive_stale(platform, slug, live)
    # The fetched count tells an empty board (a stale slug, say) from one
    # whose postings were all filtered out
    print(f"  {company}: {len(postings)} fetched, {new} new, "
          f"{rewritten} rewritten, {len(live)} live, {cut_pay} cut by pay "
          f"floor, {archived} archived")
    return new

def main():
    configure_from_file(configure)
    if not CONFIG.exists():
        print("No boards.json found — copy boards.example.json and "
              "add your boards.")
        return 1
    OUT_DIR.mkdir(exist_ok=True)

    boards = json.loads(CONFIG.read_text(encoding="utf-8"))
    new_jobs = failed = 0
    sources, malformed = set(), False
    # One bad entry or board never stops the others; any failure makes
    # the exit code non-zero so the scheduled run logs it
    for board_cfg in boards:
        try:
            sources.add(board_source(board_cfg))
        except ValueError:
            malformed = True
        try:
            new_jobs += sync_board(board_cfg)
        except Exception as e:
            name = ((board_cfg.get("company") or board_cfg.get("slug"))
                    if isinstance(board_cfg, dict) else None)
            print(f"  {name or repr(board_cfg)}: FAILED ({e}) — check "
                  f"slug/platform")
            failed += 1

    # A malformed entry's files would look unconfigured, so nothing is
    # archived as unconfigured until every entry can be read
    if malformed:
        print("  Files of boards no longer in boards.json: not checked, "
              "because an entry is malformed.")
    else:
        for source, n in sorted(archive_unconfigured(sources).items()):
            print(f"  {source}: not in boards.json, {n} file(s) archived")

    print(f"\nBoards: wrote {new_jobs} new postings to {OUT_DIR}/")
    if failed:
        print(f"[ERROR] {failed} of {len(boards)} board(s) failed.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
    