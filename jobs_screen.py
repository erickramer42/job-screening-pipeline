#!/usr/bin/env python3
"""
jobs_screen.py — parse digest emails from raw/ and board postings from
raw_boards/, apply hard filters,
rank survivors with a local LLM (Ollama), write a daily shortlist.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date
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

# ---------- Tier 1: deterministic hard filters ----------
# Both lists live in config. Employers are plain names matched as whole
# words, so a short name like "AB" doesn't cut "Fabrikam"
EXCLUDED_EMPLOYER_RE = re.compile(
    "|".join(rf"\b{re.escape(e)}\b" for e in CFG["excluded_employers"])
    or r"(?!)",  # empty list excludes nothing
    re.I,
)
# Title patterns are regex (JSON-escaped in config: "\\b" for \b).
# NOTE: these are triage cuts for niche-tool roles you don't target.
# Adjust freely — nothing here is sacred.
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

def parse_jobs():
    """Jobs arrive as 4-line quadruples ending in 'View':
    title / employer / location / View."""
    jobs, seen = [], set()
    for src_dir, pattern in SOURCE_DIRS:
        if not src_dir.exists():
            continue
        for f in sorted(src_dir.glob(pattern)):
            raw_lines = [ln.strip() for ln in
                         f.read_text(encoding="utf-8", errors="replace")
                         .splitlines() if ln.strip()]
            url, meta_company = parse_metadata(raw_lines)
            for i, ln in enumerate(raw_lines):
                if ln.lower() != "view" or i < 3:
                    continue
                title, employer, location = (raw_lines[i-3],
                                              raw_lines[i-2],
                                              raw_lines[i-1])
                if any(x.lower() in KNOWN_NOISE
                       for x in (title, employer, location)):
                    continue
                key = (title.lower(), employer.lower())
                if key in seen:
                    continue
                seen.add(key)
                jobs.append({
                    "title": title,
                    "employer": employer,
                    "location": location,
                    "source_file": f.name,
                    "url": url,
                    "source": src_dir.name,
                })
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

def score_batch(batch):
    listing = "\n".join(
        f'{i}: {j["title"]} | {j["employer"]} | {j["location"]}'
        for i, j in enumerate(batch)
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Score every job below for fit. Respond with STRICT JSON "
                'only, format: {"scores": [{"index": <int>, "score": '
                '<int 0-100>, "reason": "<one short sentence>"}]}. '
                'In your response, at most 2 of the scores may be >= 75.'
                f"Include every index exactly once.\n\nJobs:\n{listing}"
            )},
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
        data = json.load(resp)
    content = json.loads(data["message"]["content"])
    return {s["index"]: s for s in content.get("scores", [])}

def main():
    jobs = parse_jobs()
    if not jobs:
        print("No job rows parsed from raw/. Did you re-ingest with the "
              "new extractor?")
        sys.exit(1)
    kept, rejected = hard_filter(jobs)
    print(f"Parsed {len(jobs)} unique listings; "
          f"{len(rejected)} cut by hard filters, {len(kept)} to rank.")

    # Batch to keep prompts reasonable for local models
    BATCH = 30
    # Consecutive failures mean Ollama is down or the model is missing;
    # stop instead of sitting through every remaining batch's timeout
    MAX_FAIL_STREAK = 3
    for i, j in enumerate(kept):
        try:
            j["score"] = 0
            j["reason"] = "unscored"
        except Exception:
            pass

    n_batches = (len(kept) + BATCH - 1) // BATCH
    print(f"Scoring {len(kept)} listings with {MODEL} "
          f"({n_batches} batches)...")
    ok = streak = 0
    for start in range(0, len(kept), BATCH):
        batch = kept[start:start + BATCH]
        try:
            scores = score_batch(batch)
            for idx, s in scores.items():
                try:
                    j = batch[int(idx)]
                    j["score"] = int(s.get("score", 0))
                    j["reason"] = s.get("reason", "")
                except (ValueError, IndexError):
                    continue
            print(f"  batch {start // BATCH + 1}: "
                  f"{len(scores)}/{len(batch)} scored")
            ok += 1
            streak = 0
        except Exception as e:
            print(f"  batch {start // BATCH + 1} FAILED: {e}")
            streak += 1
            if streak >= MAX_FAIL_STREAK:
                print(f"  {streak} consecutive failures; aborting scoring.")
                break

    # Unattempted batches after an abort count as failed
    failed = n_batches - ok
    if n_batches and not ok:
        # Write nothing: a missing shortlist is the failure signal for
        # unattended runs, and an all-zero one would look like a quiet day
        print(f"[ERROR] No batch scored. Is Ollama running at {OLLAMA_URL} "
              f"with {MODEL} pulled? No shortlist written.")
        # Exit 2, not 1: nothing was written, so a retry is safe and
        # often helps (Ollama still starting). The scheduled wrapper
        # retries only on 2, never after a partial run
        sys.exit(2)

    kept.sort(key=lambda j: j["score"], reverse=True)

    OUT_DIR.mkdir(exist_ok=True)
    stamp = date.today().isoformat()
    json_path = OUT_DIR / f"ranked_{stamp}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"ranked": kept, "rejected": rejected,
                   "failed_batches": failed}, f, indent=2)

    md_path = OUT_DIR / f"shortlist_{stamp}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Daily Shortlist — {stamp}\n\n")
        if failed:
            f.write(f"**WARNING: {failed} of {n_batches} scoring batches "
                    f"failed; those listings are ranked as 0/unscored.**"
                    f"\n\n")
        f.write(f"{len(kept)} ranked, {len(rejected)} auto-cut. "
                f"Board postings link directly; digest rows need the email.\n\n")
        f.write("| # | Score | Title | Employer | Location | Link | Reason |\n")
        f.write("|---|-------|-------|----------|----------|------|--------|\n")
        for i, j in enumerate(kept[:25], 1):
            title = j["title"].replace("|", "/")
            reason = j["reason"].replace("|", "/")[:80]
            link = j.get("url", "") or "-"
            f.write(f"| {i} | {j['score']} | {title} | {j['employer']} | "
                    f"{j['location']} | {link} | {reason} |\n")

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path} (top 25 of {len(kept)})")
    if failed:
        print(f"[ERROR] {failed} of {n_batches} batches failed; "
              f"shortlist is incomplete.")
        sys.exit(1)

if __name__ == "__main__":
    main()
    