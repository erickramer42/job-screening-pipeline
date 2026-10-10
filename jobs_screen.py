#!/usr/bin/env python3
"""
jobs_screen.py — parse digest emails from raw/ and board postings from
raw_boards/, apply hard filters and a digest recency window, rank with
a local LLM (Ollama) in two passes, write a daily shortlist.

Pass 1: all listings, title/employer/location only (blind triage).
Pass 2 (rerank): the top N description-bearing rows re-scored with a
role-marker excerpt from the stored posting text, so blind-pass
ambiguity (opaque titles, non-tech employers) can be resolved with
actual requirements. Pass-1 score is preserved as pass1_score.
"""
import http.client
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CONFIG_FILE = Path("pipeline_config.json")

def load_config():
    if not CONFIG_FILE.exists():
        print("[ERROR] pipeline_config.json missing — copy "
              "pipeline_config.example.json and fill in your values.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

CFG = load_config()
PROMPT_PATH = Path(CFG.get("prompt_path", "screening_prompt.txt"))
# Digest rows from files whose Date header is older than this many
# days are rejected as stale before ranking. Board postings are exempt:
# their freshness is enforced by the archive mechanism in
# board_fetch.py, not by age. 0 or missing disables the window.
RECENCY_DAYS = int(CFG.get("recency_days", 0))
# Second pass: how many top description-bearing rows to re-score,
# how many per request, and the excerpt cap fed to the model.
RERANK_TOP_N = int(CFG.get("rerank_top_n", 15))
RERANK_BATCH = int(CFG.get("rerank_batch", 5))
EXCERPT_CHARS = int(CFG.get("excerpt_chars", 1500))
# Glob pattern per source dir: digests are job_uid_<UID>.txt; board
# postings are <platform>_<id>.txt (greenhouse_123, ashby_abc, ...)
SOURCE_DIRS = [
    (Path("raw"), "job_*.txt"),
    (Path("raw_boards"), "*.txt"),
]
OUT_DIR = Path("out")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("SCREEN_MODEL", "gemma4:12b")

# Table headers that would otherwise parse as a listing; header/menu
# rows specific to your digest emails come from config
KNOWN_NOISE = ({"view", "job title", "employer", "location", "action"}
               | {x.lower() for x in CFG.get("digest_noise_lines", [])})

# Board files store the posting description below this marker. Quadruple
# parsing stops at it, so description prose (which can contain "View" or
# anything else) can never be misread as a listing. Digest files have no
# marker and parse exactly as before.
DESC_MARKER = "--- DESCRIPTION ---"

# Role-content markers for pass-2 excerpts: postings commonly front
# company/investor boilerplate and bury the role, so prefix-only
# truncation would score the press release, not the job
ROLE_MARKERS = re.compile(
    r"about the role|what you['’]?ll (be )?do|what you will do|"
    r"responsibilit|requirement|qualification|what we look for|"
    r"position overview|job ?description|your impact|who you are|"
    r"in this role|the role\b",
    re.I,
)
# Legal and pay text that usually closes a posting. Role markers inside
# it ("...based on qualifications, merit and business need", "the base
# salary range for the role") are not role content. Only matches past
# FOOTER_MIN_FRACTION of the text count, so a pay line at the top of a
# posting doesn't hide everything after it.
FOOTER_MARKERS = re.compile(
    r"equal (employment )?opportunit|\beeo\b|reasonable accommodation|"
    r"(salary|pay|compensation) range|base salary",
    re.I,
)
FOOTER_MIN_FRACTION = 0.4
# A marker in running text (not a heading) only counts in the first half
# of the description; later ones are mostly closing text ("...until the
# role is filled")
RUNNING_TEXT_MAX_FRACTION = 0.5

# ---------- Tier 1: deterministic hard filters ----------
EXCLUDED_EMPLOYER_RE = re.compile(
    "|".join(rf"\b{re.escape(e)}\b" for e in CFG["excluded_employers"])
    or r"(?!)",  # empty list excludes nothing
    re.I,
)
EXCLUDE_TITLE_PATTERNS = CFG["excluded_title_patterns"]

def parse_metadata(lines):
    """Pull URL/Company from the '# Metadata' block if present."""
    url, company = "", None
    if lines and lines[0] == "# Metadata":
        for ln in lines:
            if ln.startswith("URL: "):
                url = ln[5:].strip()
            elif ln.startswith("Company: "):
                company = ln[9:].strip()
    return url, company

def parse_metadata_date(lines):
    """Date header from a digest file's metadata block, as an aware
    datetime. Board files have no Date: line, so this returns None for
    them; unparsable digest dates also return None (callers keep those
    rows rather than silently dropping them)."""
    if lines and lines[0] == "# Metadata":
        for ln in lines:
            if ln == "---":
                break
            if ln.startswith("Date: "):
                try:
                    dt = parsedate_to_datetime(ln[6:].strip())
                except (TypeError, ValueError):
                    return None
                if dt is None:
                    return None
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
    return None

def role_excerpt(description, max_chars):
    """Best scoring window from a posting description, capped hard.

    Starts at a role-content marker, since company boilerplate usually
    comes first. Markers inside the closing legal/pay text are ignored.
    A marker that starts its own line (a section heading) wins; failing
    that, one in running text within the first half. Prefix fallback
    when no marker qualifies: some signal beats none.
    """
    if not description:
        return ""
    footer = FOOTER_MARKERS.search(
        description, int(len(description) * FOOTER_MIN_FRACTION))
    limit = footer.start() if footer else len(description)
    starts = [m.start()
              for m in ROLE_MARKERS.finditer(description, 0, limit)]
    headings = [s for s in starts if s == 0 or description[s - 1] == "\n"]
    early = [s for s in starts
             if s < len(description) * RUNNING_TEXT_MAX_FRACTION]
    start = (headings or early or [0])[0]
    excerpt = description[start:start + max_chars]
    if len(description) - start > max_chars:
        # Cut at a sentence/paragraph boundary rather than mid-word
        cut = max(excerpt.rfind(". "), excerpt.rfind("\n"))
        if cut > max_chars // 2:
            excerpt = excerpt[:cut + 1]
    return excerpt.strip()

# Rank of an undated digest copy among duplicates: treated as newest,
# so a listing with an unreadable Date is kept rather than aged out
NEWEST = datetime.max.replace(tzinfo=timezone.utc)

def copy_rank(row):
    """Which duplicate copy of a listing to keep (higher wins): a board
    row (direct link, pay, description) over any digest row, then the
    newest digest."""
    return (row["source"] == "raw_boards", row["_dt"] or NEWEST)

def parse_jobs():
    """Jobs arrive as 4-line quadruples ending in 'View':
    title / employer / location / View.

    A listing repeated across files (weekly digests overlap heavily, and
    a posting can be both in a digest and on a board) is kept once per
    (title, employer), choosing the copy by copy_rank. Staleness is
    judged on the kept copy, so a listing still advertised in a recent
    digest is never aged out because an older digest also had it."""
    best = {}
    for src_dir, pattern in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for f in sorted(src_dir.glob(pattern)):
            all_lines = f.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
            try:
                cut = next(i for i, ln in enumerate(all_lines)
                           if ln.strip() == DESC_MARKER)
                desc = "\n".join(all_lines[cut + 1:]).strip()
            except StopIteration:
                cut, desc = len(all_lines), ""
            raw_lines = [ln.strip() for ln in all_lines[:cut]
                         if ln.strip()]
            url, meta_company = parse_metadata(raw_lines)
            digest_dt = (parse_metadata_date(raw_lines)
                         if src_dir.name == "raw" else None)
            for i, ln in enumerate(raw_lines):
                if ln.lower() != "view" or i < 3:
                    continue
                title, employer, location = (raw_lines[i-3],
                                              raw_lines[i-2],
                                              raw_lines[i-1])
                if any(x.lower() in KNOWN_NOISE
                       for x in (title, employer, location)):
                    continue
                row = {
                    "title": title,
                    "employer": employer,
                    "location": location,
                    "source_file": f.name,
                    "url": url,
                    "source": src_dir.name,
                    "description": desc,
                    "_dt": digest_dt,
                }
                key = (title.lower(), employer.lower())
                if key not in best or copy_rank(row) > copy_rank(best[key]):
                    best[key] = row

    now = datetime.now(timezone.utc)
    jobs = list(best.values())
    for j in jobs:
        dt = j.pop("_dt")
        j["_undated"] = bool(RECENCY_DAYS and j["source"] == "raw"
                             and dt is None)
        j["_stale"] = bool(RECENCY_DAYS and dt is not None
                           and (now - dt).days > RECENCY_DAYS)
    return jobs

def hard_filter(jobs):
    kept, rejected = [], []
    for j in jobs:
        if EXCLUDED_EMPLOYER_RE.search(j["employer"]):
            j["reject_reason"] = "excluded employer"
            rejected.append(j)
            continue
        if any(re.search(p, j["title"], re.I) for p in EXCLUDE_TITLE_PATTERNS):
            j["reject_reason"] = "excluded title pattern"
            rejected.append(j)
            continue
        kept.append(j)
    return kept, rejected

# ---------- Tier 2: LLM ranking ----------
def load_system_prompt():
    if not PROMPT_PATH.exists():
        print(f"[ERROR] {PROMPT_PATH} missing — the calibrated "
              "ranking prompt must be present. Restore it before running.")
        sys.exit(1)
    return PROMPT_PATH.read_text(encoding="utf-8").strip()

SYSTEM_PROMPT = load_system_prompt()

# Appended to the system prompt for pass 2 only. Generic mechanics
# (how to use excerpts, small-batch behavior) belong here, like the
# JSON format instructions; the calibrated rubric stays in the file.
RERANK_ADDENDUM = """

SECOND PASS — POSTING-TEXT RE-SCORING: In this pass each listing is
followed by an excerpt from the actual job posting (position overview,
responsibilities, requirements, core tech). Use it to resolve what the
title alone could not: the real stack, the actual domain, the real
seniority band. Revise the score up OR down on this evidence. The
excerpt may start mid-document and be truncated; missing sections are
not a signal against the listing. Batches here are small (~5 listings),
so the first-pass batch-frequency guidance does not apply — score each
listing independently on its own merits."""

# Retries per request. Model output is probabilistic; a truncated or
# index-shifted response is usually fine on a second sample
MAX_ATTEMPTS = 3
# Failures of the connection itself (Ollama down, timeout, a response
# cut off mid-body). These fail the batch; everything wrong with the
# CONTENT of a response is a ValueError and gets a retry instead.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)
# Consecutive failed batches before a pass gives up (Ollama down/wedged)
MAX_FAIL_STREAK = 3

def ollama_chat(system, user):
    """One chat completion; returns the model's content string.
    Raises TRANSPORT_ERRORS on connection failure and ValueError when
    the body isn't a chat response."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA_URL.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.load(resp)  # non-JSON body: JSONDecodeError (ValueError)
    content = (data.get("message") or {}).get("content") \
        if isinstance(data, dict) else None
    if not isinstance(content, str):
        raise ValueError("Ollama response has no message content")
    return content

def coerce_entry(entry):
    """One model response entry -> (index, score, reason) or None.
    Null/missing/non-numeric fields make the entry unusable. Scores are
    clamped to 0-100; the index is range-checked in align_indices."""
    if not isinstance(entry, dict):
        return None
    try:
        idx = int(entry.get("index"))
        score = int(entry.get("score"))
    except (TypeError, ValueError):
        return None
    reason = str(entry.get("reason") or "").strip()
    return idx, max(0, min(100, score)), reason

def align_indices(rows, batch_size):
    """Map coerced rows to {index: {"score", "reason"}} for 0..n-1.

    Models sometimes number from 1. Index 0 present: 0-based, used as
    is. Index n present without 0: 1-based, shifted down by one (left
    unshifted, every score would land on the neighbouring job). Neither:
    the numbering can't be told apart, so the response is rejected
    (ValueError -> retry) rather than risk misattributed scores.
    """
    idxs = {i for i, _, _ in rows}
    if rows and 0 not in idxs:
        if batch_size not in idxs:
            raise ValueError("indices are neither 0-based nor 1-based")
        rows = [(i - 1, s, r) for i, s, r in rows]
    return {i: {"score": s, "reason": r}
            for i, s, r in rows if 0 <= i < batch_size}

def salvage_scores(text):
    """Recover scores from a response truncated mid-string.

    Cuts at the last '}' (end of the last complete entry) and closes
    the JSON structure. A '}' inside the surviving reason text makes
    the repaired string invalid, which raises out and lets the caller
    retry — so this can never fabricate entries.
    """
    last = text.rfind("}")
    if last == -1:
        raise ValueError("no complete entry in response")
    return json.loads(text[:last + 1] + "]}")

def parse_scores(text, batch_size, attempt):
    """Model content -> {index: {"score", "reason"}}. Raises ValueError
    for anything unusable: empty or unsalvageable text, a body that
    isn't {"scores": [...]}, or indices that can't be aligned."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if not text.strip():
            # Zero-char / whitespace response: failure mode observed
            # 2026-10-10 (batch 7)
            raise ValueError("empty response from model")
        print(f"    [attempt {attempt}] truncated response "
              f"({len(text)} chars, tail: {text[-60:]!r}) — salvaging")
        parsed = salvage_scores(text)
    # format=json still allows a top-level array or "scores": null
    if not isinstance(parsed, dict) \
            or not isinstance(parsed.get("scores"), list):
        raise ValueError('response is not {"scores": [...]}')
    rows = [r for r in map(coerce_entry, parsed["scores"]) if r]
    return align_indices(rows, batch_size)

def request_scores(system, user, batch):
    """Score one batch with retries. Returns {index: {"score",
    "reason"}}: complete as soon as one attempt covers every index,
    otherwise the most complete attempt (possibly {}). Every problem
    with a response's content is retried; TRANSPORT_ERRORS propagate."""
    best = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            scores = parse_scores(ollama_chat(system, user), len(batch),
                                  attempt)
        except ValueError as e:
            print(f"    [attempt {attempt}] unusable response ({e}) "
                  f"— retrying")
            continue
        if len(scores) > len(best):
            best = scores
        missing = len(batch) - len(scores)
        if not missing:
            return scores
        print(f"    [attempt {attempt}] {missing} indices missing "
              f"— retrying")
    return best

def score_batch(batch):
    """Pass 1: blind triage on title/employer/location."""
    listing = "\n".join(
        f'{i}: {j["title"]} | {j["employer"]} | {j["location"]}'
        for i, j in enumerate(batch)
    )
    user = ("Score every job below for fit. Respond with STRICT JSON "
            'only, format: {"scores": [{"index": <int>, "score": '
            '<int 0-100>, "reason": "<one short sentence>"}]}. '
            'In your response, at most 2 of the scores may be >= 75. '
            f"Include every index exactly once.\n\nJobs:\n{listing}")
    return request_scores(SYSTEM_PROMPT, user, batch)

def score_rerank(batch):
    """Pass 2: re-score with posting-text excerpts. Smaller batches
    mean the 'at most 2 >= 75' frequency cap is dropped (it was
    calibrated for batches of 30 and would strangle a 5-item batch)."""
    blocks = []
    for i, j in enumerate(batch):
        ex = role_excerpt(j["description"], EXCERPT_CHARS)
        blocks.append(f'{i}: {j["title"]} | {j["employer"]} | '
                      f'{j["location"]}\nPOSTING: {ex}')
    user = ("Re-score every job below for fit using the posting text. "
            "Respond with STRICT JSON "
            'only, format: {"scores": [{"index": <int>, "score": '
            '<int 0-100>, "reason": "<one short sentence>"}]}. '
            f"Include every index exactly once.\n\nJobs:\n"
            + "\n---\n".join(blocks))
    return request_scores(SYSTEM_PROMPT + RERANK_ADDENDUM, user, batch)

def run_batches(label, batches, scorer, apply):
    """Score batches in order with scorer(batch) and hand usable
    results to apply(batch, scores). A batch whose request fails
    (TRANSPORT_ERRORS) or yields no usable scores is left as-is and
    counted as failed; MAX_FAIL_STREAK failures in a row abort the pass.
    Returns (ok, failed, aborted); batches never attempted after an
    abort count as failed."""
    ok = streak = 0
    for n, batch in enumerate(batches, 1):
        try:
            scores = scorer(batch)
            error = (None if scores else
                     f"no usable scores after {MAX_ATTEMPTS} attempts")
        except TRANSPORT_ERRORS as e:
            scores, error = {}, e
        if error:
            print(f"  {label} {n} FAILED: {error}")
            streak += 1
            if streak >= MAX_FAIL_STREAK:
                print(f"  {streak} consecutive failures; aborting.")
                return ok, len(batches) - ok, True
            continue
        apply(batch, scores)
        tag = ("" if len(scores) == len(batch)
               else f" (partial after {MAX_ATTEMPTS} attempts)")
        print(f"  {label} {n}: {len(scores)}/{len(batch)} scored{tag}")
        ok += 1
        streak = 0
    return ok, len(batches) - ok, False

def apply_pass1(batch, scores):
    for idx, s in scores.items():
        batch[idx]["score"] = s["score"]
        batch[idx]["reason"] = s["reason"]

def apply_rerank(batch, scores):
    for idx, s in scores.items():
        j = batch[idx]
        j["pass1_score"] = j["score"]
        j["score"] = s["score"]
        j["reason"] = s["reason"]
        j["rescored"] = True

def main():
    jobs = parse_jobs()
    if not jobs:
        print("No job rows parsed from raw/ or raw_boards/. Did you "
              "re-ingest with the new extractor?")
        sys.exit(1)

    fresh, stale_rows, undated = [], [], 0
    for j in jobs:
        stale, no_date = j.pop("_stale"), j.pop("_undated")
        if stale:
            j["reject_reason"] = f"stale digest (> {RECENCY_DAYS} days old)"
            stale_rows.append(j)
        else:
            undated += no_date
            fresh.append(j)

    if RECENCY_DAYS:
        print(f"Recency window: {len(stale_rows)} digest rows past "
              f"{RECENCY_DAYS} days excluded")
        if undated:
            print(f"[WARN] {undated} digest rows from files with an "
                  f"unreadable Date header were KEPT (not aged out)")

    kept, rejected_hf = hard_filter(fresh)
    rejected = stale_rows + rejected_hf
    print(f"Parsed {len(jobs)} unique listings; {len(rejected)} cut "
          f"({len(stale_rows)} stale, {len(rejected_hf)} hard filters), "
          f"{len(kept)} to rank.")

    for j in kept:
        j["score"] = 0
        j["reason"] = "unscored"

    BATCH = 30
    batches = [kept[s:s + BATCH] for s in range(0, len(kept), BATCH)]
    n_batches = len(batches)
    print(f"Pass 1: scoring {len(kept)} listings with {MODEL} "
          f"({n_batches} batches)...")
    ok, failed, aborted = run_batches("batch", batches, score_batch,
                                      apply_pass1)
    if n_batches and not ok:
        print(f"[ERROR] No batch scored. Is Ollama running at {OLLAMA_URL} "
              f"with {MODEL} pulled? No shortlist written.")
        # 2, not 1: nothing was written, so run_screen.bat can safely
        # retry (Ollama often just hasn't started yet)
        sys.exit(2)

    kept.sort(key=lambda j: j["score"], reverse=True)

    # ---- Pass 2: re-score top description-bearing rows ----
    # Failed re-score batches keep their pass-1 scores
    rerank_failed = 0
    cand = [j for j in kept if j.get("description")][:RERANK_TOP_N]
    if cand:
        chunks = [cand[s:s + RERANK_BATCH]
                  for s in range(0, len(cand), RERANK_BATCH)]
        if aborted:
            # Pass 1 gave up on a dead or wedged Ollama; each re-score
            # request would only sit out its own timeout
            print(f"Pass 2 skipped: pass 1 aborted. {len(chunks)} "
                  f"re-score batches not attempted.")
            rerank_failed = len(chunks)
        else:
            print(f"Pass 2: re-scoring top {len(cand)} description-bearing "
                  f"rows with posting text ({len(chunks)} batches)...")
            _, rerank_failed, _ = run_batches("rerank batch", chunks,
                                              score_rerank, apply_rerank)
            kept.sort(key=lambda j: j["score"], reverse=True)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    json_path = OUT_DIR / f"ranked_{stamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"ranked": kept, "rejected": rejected,
                   "failed_batches": failed,
                   "rerank_failed_batches": rerank_failed}, f, indent=2)

    md_path = OUT_DIR / f"shortlist_{stamp}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Daily Shortlist — {stamp}\n\n")
        if failed:
            f.write(f"**WARNING: {failed} of {n_batches} pass-1 batches "
                    f"failed; those listings are ranked as 0/unscored.**"
                    f"\n\n")
        if rerank_failed:
            f.write(f"**NOTE: {rerank_failed} re-scoring batch(es) failed "
                    f"or were skipped; those rows kept their pass-1 "
                    f"score.**\n\n")
        f.write(f"{len(kept)} ranked, {len(rejected)} auto-cut. "
                f"Board postings link directly; digest rows need the "
                f"email. Rows marked ▲ were re-scored against the "
                f"posting text.\n\n")
        f.write("| # | Score | Title | Employer | Location | Link | Reason |\n")
        f.write("|---|-------|-------|----------|----------|------|--------|\n")
        for i, j in enumerate(kept[:25], 1):
            title = j["title"].replace("|", "/")
            mark = "▲ " if j.get("rescored") else ""
            reason = j["reason"].replace("|", "/")[:80]
            link = j.get("url", "") or "-"
            f.write(f"| {i} | {mark}{j['score']} | {title} | "
                    f"{j['employer']} | {j['location']} | {link} | "
                    f"{reason} |\n")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path} (top 25 of {len(kept)})")
    if failed:
        print(f"[ERROR] {failed} of {n_batches} batches failed; "
              f"shortlist is incomplete.")
        sys.exit(1)

if __name__ == "__main__":
    main()
    