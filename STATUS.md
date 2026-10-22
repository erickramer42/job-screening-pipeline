# STATUS.md — Job Screening Pipeline

## Overview

This project automates discovery, triage, and ranking of job postings from two sources:

1. **Job-alert digest emails**: saved-search emails from a job site, nightly since mid-October 2026, read from the candidate's mailbox.
2. **Company career boards**: public job-board APIs (Greenhouse, Lever, Ashby, SmartRecruiters) for the companies listed in `boards.json`.

The goal is a focused daily application routine (~10 highest-quality matches per day). Applications are not submitted automatically.

Setup, usage, and the configuration reference are in [README.md](README.md). This file tracks project state and planned work.

### Philosophy
- **Human-in-the-loop:** Agent discovers and ranks; the candidate applies manually at company career pages
- **No scraping:** digest data comes only from emails delivered to the candidate's inbox, not from scraping the job site. Board data comes from the job-board platforms' public, unauthenticated posting APIs, not from scraping HTML career pages.
- **Deterministic-first filtering:** Hard filters remove obvious noise before LLM scoring
- **Safe by default:**
  - Mail access is read-only, and ingest stops on folder errors.
  - Emails are never modified or deleted.
  - Scripts exit non-zero on failure.
- **Private by default:** personal settings live only in gitignored files, and credentials only in environment variables.

---

## Current State

### Working
| Component | Status | Notes |
|-----------|--------|-------|
| Email ingest (`job_ingest.py`) | ✅ Operational | Fetches digest emails over IMAP into `raw/job_uid_<UID>.txt`. Read-only (EXAMINE + `BODY.PEEK[]`). |
| Board fetch (`board_fetch.py`) | ✅ Operational | Pulls `boards.json` companies into `raw_boards/` and filters by title, metro-area/US-remote location, and pay floor. Stores each posting's description (Greenhouse, Ashby, SmartRecruiters; not Lever). Closed postings move to `raw_boards/archive/`; files from an older format are rewritten automatically. |
| Slug discovery (`slug_probe.py`) | ✅ Operational | Guesses board slugs for company names (e.g. `targets.txt`) and prints `boards.json` lines for slugs that return jobs |
| Job parsing (`jobs_screen.py` parser) | ✅ Operational | Reads title/employer/location quadruples from `raw/` and `raw_boards/`. Keeps one copy per listing (board copy first, then newest digest) and cuts digests older than `recency_days`. |
| Hard filters | ✅ Operational | Blocks the employers and title patterns listed in `pipeline_config.json` |
| LLM ranking | ✅ Operational | Pass 1 scores every survivor 0–100 from title/employer/location. Pass 2 re-scores the top `rerank_top_n` listings that have a description, using a role-section excerpt. Thinking is off and the first attempt is greedy, so scores are repeatable; employer score caps are enforced after each pass. Malformed responses are retried; 3 consecutive failed batches stop a pass. |
| Output generation | ✅ Operational | Produces `out/shortlist_YYYY-MM-DD.md` and JSON. Board rows include direct posting links; ▲ marks pass-2 scores. |
| Scheduled runs | ✅ Operational | Task Scheduler runs ingest at 01:00 and 05:00, and board fetch + screening at 06:00. Tasks wake the computer, run after a missed start, and the wrappers retry (see [Automation](#automation)). Verified across 2026-10-09 to 10-22, with two misses. On 10-14 no task ran at all, and the missed-start catch-up didn't fire. On 10-22 screening failed all 3 attempts because Ollama wasn't running. Both are open items (see Next Milestones). |

### Recent Changes (2026-10-22)
Quick fixes and score stabilization from a code review.
- **Ingest:**
  - IMAP TLS certificates are now verified. `imaplib` doesn't verify them unless it's given a context.
  - `imap_tls_verify: false` turns verification off for a local bridge with a self-signed certificate. It's refused for any host that isn't loopback.
  - `imap_cafile` trusts a specific certificate instead of the system store.
  - Ingest no longer writes the unused `Job_ID` line or creates the unused `processed/` folder.
- **Board fetch:** a malformed `boards.json` entry, or a board whose response doesn't have the expected shape, is reported and skipped instead of stopping the run. A failed board's files aren't archived. The script exits 1 when any board failed.
- **Screener:**
  - Every shortlist cell is escaped, so a `|` in a location ("United States | Remote") no longer breaks the table row.
  - A row's link is read only from the file's metadata block.
  - The ranking prompt now explains the pay range in parentheses on board locations.
- **Slug discovery:**
  - Auto mode reads the latest dated `ranked_<date>.json`. It had been picking an older `ranked_v1_…` file, which sorts last.
  - Probes start with the plain company name, and only whole corporate words are stripped. Before, "Cisco" was probed as `cis` first.
- **Scoring stability:**
  - **Thinking off:** model thinking is now off for every request (`model_thinking`, default `false`).
    - With thinking on, the default model reasoned past the 8k context and returned nothing. That caused the "empty"/"truncated response" retries on 9 of 11 runs.
    - With it off, a pass-1 batch takes ~18 s instead of 60–90 s.
  - **Greedy first attempt:** the first request for each batch is greedy (temperature 0, seed 42), and retries sample with a seed per attempt. On the same input, two runs now differ by 1.2 points on average; day to day it used to be 6.0.
  - **Score caps:** with thinking off, the model follows the prompt's caps less reliably. The new `score_caps` (first added as employer-only `employer_score_caps`) enforces the mechanical ones in code, and `capped_from` keeps the model's own score.
  - **Area filter:** digest listings outside the search area are cut before ranking (`outside area`), with the same rule board fetch uses, now shared as `location_filter.py`. On Oct 22 data that's 62 rows, which the model had been scoring 0.
  - **Mixed batches:** pass-1 batches are cut in a fixed hash order, so one employer's listings no longer fill a batch. The largest single-employer cluster in a batch went from 26 to 10.
  - **Runtime:** a full screening run on Oct 22 data took 9.4 min, down from ~25.
- **Shortlist feedback (first review, on the 10-22 list):**
  - **Ranking prompt:**
    - Side-project AI work is no longer treated as professional experience.
    - Getting hired quickly is the stated priority.
    - Some role types are named as stretches.
    - Security-clearance requirements and notoriously hard interview loops are discounted.
  - **`score_caps`** replaces the employer-only caps. A rule can match employer, title, or posting text, and can carry a flag shown in a new Flags column. New rules: required clearance (flag `clearance`), staff/principal titles, and reach employers (flag `reach`).
  - **`shortlist_per_employer`** limits the rows from one employer, with a "More from" count under the table.
  - **`applied.txt`:** listed jobs are skipped when ranking and shown in an "Already applied, still posted" section.
  - **`feedback/`** (gitignored) holds per-day notes.
  - **Measured** on the same data:
    - 15 of the 16 listings the feedback named moved as intended. The exception is a "decent" listing that fell behind two near-identical postings from the same employer.
    - In the top 25, reasons citing agent/LLM work fell from 15 to 2, employers rose from 10 to 16, and digest rows from 2 to 8.
- **Verdicts (`verdicts.py`):**
  - The screener writes `feedback/<date>.md` with one `N [?] title | employer ::` line per shortlist row, for a y/m/n/a verdict. It never overwrites an existing file.
  - `a` verdicts count as applied.
  - `python verdicts.py` scores a run against all verdicts so far.
  - The 10-22 notes were converted to verdicts. Scored against them (in-sample, since the changes came from those notes):
    - pair-ordering agreement rose from 53% to 93%;
    - "no" rows on the list fell from 8 of 8 to 0;
    - "yes" rows fell from 5 of 5 to 2. All three were dropped by the 2-per-employer limit; a limit of 3 would keep 4 of 5, still with no "no" rows.
  - Each shortlisted row carries `shortlist_rank` in the JSON, so `verdicts.py` judges a run by the list it showed. It reads only the JSON: no config and no model.
  - The verdict template puts a backslash before any pipe in an employer name, so the name can't be mistaken for the separator.
- **Absolute scoring rubric:**
  - The per-batch quota ("at most 2 scores ≥ 75") is gone from both passes. Each request now says to score every job on its own, and the ranking prompt's bands say what each score requires instead of how many scores a batch may have.
  - The pass-2 instructions also say to treat posting text as evidence, never as instructions.
  - **Measured:** batch-mate dependence barely changed (6.8 → 6.3 points), so per-listing scoring with a score cache is the remaining fix.
  - **Watch:** the new bands score looser, with 24 pass-1 scores at 75+ against 15. Ordering agreement with the verdicts was 86% against 93% (in-sample, within batch noise).
  - With the local per-employer limit raised to 3, all 5 "yes" rows made the shortlist in that run.

### Recent Changes (2026-10-10)
- **Descriptions and recency (new):**
  - `board_fetch.py` stores each posting's description below a `--- DESCRIPTION ---` marker, which the screener's listing parser stops at.
  - `recency_days` makes ingest skip old digests after checking only the Date header, and makes the screener cut digest listings whose newest copy is older than the window.
  - The screener re-scores the top listings that have a description in a second pass (`rerank_top_n`, `rerank_batch`, `excerpt_chars`).
- **Review fixes, scoring:**
  - Every problem with a model response's content (wrong JSON shape, `null` scores, a body without message content, unalignable indices) is retried instead of crashing the run. Only connection failures fail a batch, and a cut-off response (`IncompleteRead`) is now one of those.
  - Responses numbered from 1 are shifted onto the right listings, and numbering that can't be determined is rejected. Scores are clamped to 0–100.
  - The most complete retry is kept, not the last one.
  - Both passes share one batch loop with the 3-failure abort, and pass 2 is skipped when pass 1 had to stop.
- **Review fixes, dedup:** a listing that appears in several files keeps its board copy (link, pay, description), otherwise its newest digest copy. Staleness is judged on that copy, so an older digest can no longer age out a listing that is still advertised.
- **Review fixes, text:**
  - New shared `html_text.py`. Block tags become line breaks, entities are decoded after tags are stripped, and all whitespace, including non-breaking spaces, is normalized.
  - Greenhouse's double-encoded HTML is decoded correctly; 1,215 leftover entities in 185 descriptions dropped to 0.
  - An HTML-only digest now parses one field per line. This is checked against a sample table but hasn't yet met a real HTML-only digest.
  - Pass-2 excerpts:
    - start at a section heading (including curly-apostrophe "What you’ll do", "Job description", "Who you are");
    - ignore marker words inside closing EEO or pay text;
    - use running-text matches only from the first half.
    - On the current 185 postings, excerpts starting in EEO or pay text went from 6 to 0, and those starting at a heading from 50 to 174.
  - Board files carry a `Format:` line; older files are rewritten on the next run, and a failed SmartRecruiters description fetch is retried.
- **Review fixes, ingest:** header and body fetches find the message bytes even when the server sends an unsolicited FETCH line or a non-literal field first.
- **Cleanup:**
  - Removed dead error handlers and a duplicated abort block.
  - Fixed a missing space in the pass-1 instructions (">= 75.Include").
  - Board fetch converts descriptions only for postings it writes.
  - The bare `hardware`, `electrical`, `civil` and `mechanical` title patterns are anchored with `\b`, so `civil` no longer matches "Civilian".

### Recent Changes (2026-10-09)
- **Ingest:**
  - IMAP UIDs replace sequence numbers in filenames (one-time re-ingest done).
  - `Job_ID` now hashes the Date header too.
  - Mail access is read-only: emails are no longer marked read, and nothing can be expunged.
  - Exits 1 on folder, search, or fetch errors.
  - IMAP host, port, and STARTTLS are config settings instead of being inferred from the host name.
  - Credentials come from environment variables named in the config (`email_user_env`, `email_password_env`). Ingest refuses a value that isn't a variable name, without printing it.
  - IMAP connections time out after 60 seconds, so an unresponsive server fails the run instead of hanging it.
- **Screener:**
  - No shortlist is written if no batch scores (e.g. Ollama down). It exits 2 in that case, so the scheduled wrapper can retry safely.
  - Partial failures write the shortlist with a WARNING banner and `failed_batches` in the JSON, and exit 1.
  - The ranking prompt is loaded from the file at `prompt_path`.
  - Employer exclusions match whole words, so a short name can't cut an unrelated employer whose name contains it.
- **Board fetch (new):**
  - Ashby pay is read from `compensation.summaryComponents` (USD salary only; hourly rates ×2080).
  - Title blocks are word-bounded, so "Internal Tools" and "Sales Platform" roles survive.
  - Location uses the platforms' structured remote/country fields; cities outside `metro_cities` and non-US remote are dropped.
  - SmartRecruiters fetches every page and links to the posting page, not the API.
  - Postings that close are moved to the archive.
- **Configuration:**
  - Every personal setting moved out of code into gitignored `pipeline_config.json`: mail settings, metro cities, remote regions, pay floor, title and employer filters, digest noise lines, and the prompt path.
  - `boards.json` is gitignored too. `pipeline_config.example.json` and `boards.example.json` hold placeholders to start from.
- **Automation:**
  - Task Scheduler tasks with `cmd` wrappers; credentials moved to Windows user environment variables.
  - The tasks wake the computer, run after a missed start, and can start on battery. They have time limits (2 h ingest, 8 h screening), and a 05:00 catch-up ingest picks up late digests.
  - The wrappers retry and set UTF-8 output. They log unbuffered, so progress shows while a run is going, and each trims only its own logs.
- **Docs:** README.md added.

### Pending / Known Issues
| Issue | Priority | Impact | Planned Fix |
|-------|----------|--------|-------------|
| Pass-2 scores share a ranking with pass-1 scores | Medium | Only listings with a description (board rows) are re-scored against posting text, so board rows can crowd digest rows out of the top 25. Much smaller since 2026-10-22: 17 board and 8 digest rows, and no buried digest "yes" rows in the verdicts so far. | Separate shortlist sections, only if `verdicts.py` shows digest "yes" rows below the cut |
| Every run re-scores everything in the recency window | Medium | Each day re-scores all digest listings from the last `recency_days` plus every live board posting, though ~97% were scored the day before. A run takes ~5–9 min since 2026-10-22. | Score cache keyed by listing |
| Scores depend on batch-mates | Medium | The same listing scored in a different batch of 30 moves ~6 points on average (8% move 20+), against ~1.2 for a repeat of the same batch. The per-batch quota is gone (2026-10-22), but the model still grades partly against the other listings in its request. | Score each new listing in its own request, together with the score cache |
| Dedup key ignores location | Medium | Same title+employer in another city is dropped. The kept copy is the board row when there is one, otherwise the newest digest. | Add location to the key where the location strings are comparable |
| UIDVALIDITY not tracked | Medium | If the server resets UIDVALIDITY (e.g. a local mail server rebuilds its cache), UIDs restart at 1 and new digests are silently skipped as duplicates | Record UIDVALIDITY; warn or re-ingest when it changes |
| Silent parser failure on format change | Medium | Screener finds 0 jobs without warning | Canary check: count `View` lines per file |
| Lever descriptions not captured | Low | Lever postings never reach pass 2. No Lever boards are configured today. | Probe the Lever schema when a Lever board is added |
| `slug_probe.py` "VERIFIED" only means the slug has jobs | Low | A generic name (e.g. "Google") can match a different company's board | Check the company on the board before adding to `boards.json` |
| Remote-by-timezone locations dropped | Low | e.g. Greenhouse "Eastern or Central Timezone" fails the location filter | Add timezone phrases to the remote check if needed |

---

## File Map

```
jobScreensApplications/
├── job_ingest.py                # IMAP fetcher, read-only,
│                                #   writes digests to raw/job_uid_<UID>.txt
├── board_fetch.py               # Public board APIs -> raw_boards/<platform>_<id>.txt
│                                #   Filters: title patterns/keywords, metro cities,
│                                #   remote/US check, pay floor
├── jobs_screen.py               # Parser + recency/hard filters + two-pass
│                                #   LLM ranker -> out/
├── html_text.py                 # Shared HTML-to-text conversion (ingest + boards)
├── location_filter.py           # Shared search-area rule (boards + digest filter)
├── verdicts.py                  # Reads shortlist verdicts; scores a run against them
├── slug_probe.py                # Finds board slugs for company names
│                                #   (python slug_probe.py targets.txt)
├── pipeline_config.json         # All personal settings (gitignored)
├── pipeline_config.example.json # Placeholder config to copy
├── boards.json                  # Boards to fetch (gitignored)
├── boards.example.json          # Placeholder board list to copy
├── screening_prompt.txt         # System prompt for LLM ranking (gitignored; prompt_path)
├── targets.txt                  # Company names to probe (gitignored)
├── applied.txt                  # Jobs already applied to (gitignored)
├── feedback/                    # Daily verdict files, feedback/<date>.md (gitignored)
├── run_ingest.bat               # Task Scheduler wrappers (gitignored)
├── run_screen.bat
├── README.md                    # Setup, usage, and config reference
├── STATUS.md                    # This file: project state and roadmap
├── raw/                         # Ingested email digests (gitignored)
├── raw_boards/                  # One file per live board posting, with its
│                                #   description (gitignored)
│   └── archive/                 #   Closed/filtered-out postings; not screened
├── out/                         # Ranked shortlists, JSON + Markdown (gitignored)
└── logs/                        # Logs from scheduled runs (gitignored)
```

---

## Daily Routine (Manual Mode)

```bash
cd /path/to/jobScreensApplications
python job_ingest.py       # Downloads new digest emails to raw/
python board_fetch.py      # Refreshes raw_boards/ from boards.json
python jobs_screen.py      # Scores and writes out/shortlist_<today>.md
```

Credentials come from Windows user environment variables (see [Environment Variables](#environment-variables)), so any newly opened terminal has them.

Review `out/shortlist_<today>.md`:
- Board rows have direct posting links.
- Digest rows need the original email for their "View" links.

Apply manually at company career pages. Afterwards:
- Fill in `feedback/<date>.md`: a y/m/n/a verdict per row, with notes after `::`. `a` marks the job as applied.
- Add jobs applied to outside the shortlist to `applied.txt` (URL, or `Title | Employer` for digest rows).
- Run `python verdicts.py` after changing the prompt or config, to see whether the ranking moved toward your verdicts.
- Turn repeated notes into config rules or prompt changes (see README, "Reviewing the shortlist").

Adding boards: put company names in `targets.txt` and run `python slug_probe.py targets.txt`. Check that each VERIFIED board really belongs to that company, then paste the lines into `boards.json`.

---

## Automation

Digest emails arrive **nightly**: usually three a night since 2026-10-11 (35 from Oct 11 to 22), all between 23:59 and 00:35 local time. Before that, from Apr 20 to Oct 6, 2026, there were 23 weekly digests, arriving late Monday night between 23:00 and 01:35. Board postings change continuously, so a daily fetch is worthwhile too.

Goal: the pipeline runs unattended overnight and the shortlist is waiting in the morning.

### Schedule

| Time | Task | Wrapper | Runs |
|------|------|---------|------|
| 01:00 and 05:00 daily | `JobIngest` | `run_ingest.bat` | `job_ingest.py` |
| 06:00 daily | `JobScreen` | `run_screen.bat` | `board_fetch.py`, then `jobs_screen.py` |

The 01:00 ingest has caught every nightly digest since Oct 11, and 22 of the 23 weekly ones before that. The 05:00 run is insurance for late arrivals; it hasn't been needed since the nightly schedule started. Emails already saved to `raw/` are skipped, so the second run costs almost nothing. Board fetch runs just before screening, so the shortlist uses the freshest postings.

### Wrappers

The wrappers are gitignored. Both run under `cmd` (no Git Bash) and contain no machine-specific paths: each one changes to its own folder with `cd /d "%~dp0"`.
- **Both:**
  - Create `logs\` if it's missing, and trim their own logs to the newest 2,000 lines before running.
  - Set `PYTHONUTF8=1`. Output redirected to a file would otherwise use the ANSI code page, and a character outside it crashes the script.
  - Run Python with `-u`, so log output is written as it happens and survives a run that gets killed.
  - Write a header line to the log for each attempt.
- **`run_ingest.bat`:** up to 3 attempts, 10 minutes apart, for a mail server that's still starting after wake or login. Output goes to `logs\ingest.log`. If every attempt fails, it writes `ingest FAILED rc=<code> attempts=3` to `logs\errors.log` and exits with the script's code.
- **`run_screen.bat`:**
  - Runs board fetch, logging to `logs\boards.log`. A non-zero exit is logged to `errors.log`, and screening still runs on the postings already on disk.
  - Then runs the screener, logging to `logs\screen.log`. Only exit code 2 is retried (nothing scored, so nothing was written, usually because Ollama is still starting): up to 3 attempts, 15 minutes apart.
  - Partial failures (exit 1) aren't retried, because a retry would re-score everything. They're logged as `screen FAILED rc=<code> attempts=<n>`.
  - Exits 1 if either step failed.

### Task settings (current)
- **Trigger:** daily at the times above. **Action:** the wrapper, which sets its own working directory.
- **Wake the computer to run this task:** on. **Run as soon as possible after a missed start:** on, so a run missed while the PC was off or logged out happens at the next opportunity.
- **Can start on battery and keeps running on battery:** on.
- **Time limit:** 2 h for ingest, 8 h for screening. A full run on 2026-10-22 data (275 ranked listings, 22 batches) took 9.4 min. The limit stops a hung run before the next day's run, which would otherwise be skipped ("do not start a new instance").
- **Runs only while the user is logged on.** This is deliberate: the local mail server and Ollama run in the user's session, so a logged-off run couldn't reach them. With catch-up on, a missed run starts once the user logs on.

### Prerequisites for unattended runs
1. The IMAP server must be reachable; if it runs locally, set it to auto-start with Windows
2. Ollama service must be running — set Ollama to launch at login or install as a service
3. `EMAIL_USER` and `EMAIL_PASSWORD` must be set as Windows user environment variables (see below)
4. Check Task Scheduler's **Last Run Result** and `logs/` after the first automated nights:
   - `logs/errors.log` gets one timestamped line per failed step (`ingest FAILED`, `board fetch FAILED`, `screen FAILED`), after retries.
   - The scripts exit non-zero on failure. `board_fetch.py` tries every board first, then exits 1 if any failed.
   - **No shortlist for today** means scoring failed completely (Ollama down or model missing; see `logs/screen.log`) or a wrapper path/env-var problem.
   - **A WARNING line at the top of the shortlist** means some batches failed and those listings are ranked 0.

---

## Dependencies

| Dependency | Required for |
|------------|--------------|
| Python 3.13 | All scripts (standard library only) |
| IMAP server | Access to the candidate's mailbox |
| Internet access | `board_fetch.py` and `slug_probe.py` (public board APIs) |
| Ollama + configured model | LLM scoring |
| Windows Task Scheduler | Scheduled runs (wrappers use `cmd`, plus PowerShell for log trimming) |

### Environment Variables

Set once per Windows user with `setx`. Values are stored in the user registry in plain text, and only terminals opened afterwards see them.

```bat
setx EMAIL_USER "<IMAP login>"
setx EMAIL_PASSWORD "<IMAP password>"
rem Optional overrides
setx OLLAMA_URL "http://localhost:11434"
setx SCREEN_MODEL "<model tag, e.g. a mid-size local instruct model>"
rem Optional: UTF-8 output when you redirect a manual run to a file
rem (the wrappers already set this for scheduled runs)
setx PYTHONUTF8 1
```

IMAP host, port, STARTTLS, and folder live in `pipeline_config.json`, along with the names of the two credential variables (`email_user_env`, `email_password_env`).

---

## Tuning Notes

- **Hard filters** (applies to both sources), in `pipeline_config.json`:
  - `excluded_employers`: plain names, matched as whole words
  - `excluded_title_patterns`: regex, JSON-escaped (`"\\b"` for `\b`)
- **Board pre-filters** in `board_fetch.py`:
  - Which titles are fetched: `included_title_patterns` (regex); blocked title words: `excluded_keywords` (plain phrases, matched as whole words); both in `pipeline_config.json`
  - Metro cities: `metro_cities` in `pipeline_config.json`
  - Region names that count as US for remote postings without a country field: `remote_regions` in `pipeline_config.json`
  - Pay floor: `pay_floor` in `pipeline_config.json` (a posting is cut only when the top of its range is below the floor; unknown pay is kept)
  - Re-running after a filter change archives postings that no longer pass.
- **Boards:** edit `boards.json`; use `slug_probe.py` to find slugs
- **Ranking prompt:** edit the file at `prompt_path` (default `screening_prompt.txt`). The pass-2 addendum (`RERANK_ADDENDUM` in `jobs_screen.py`) is appended to it for re-scoring.
- **Recency window:** `recency_days` in `pipeline_config.json` (ingest and screener both read it; `0` disables)
- **Pass 2:** `rerank_top_n` (`0` disables), `rerank_batch`, and `excerpt_chars` in `pipeline_config.json`. Excerpt selection uses `ROLE_MARKERS` and `FOOTER_MARKERS` in `jobs_screen.py`.
- **Batch size:** lower `BATCH = 30` to 20 or 15 if JSON parse failures appear
- **Model choice:** swap `SCREEN_MODEL` for a smaller model (speed) or larger one (quality)
- **Board file format:** bump `FILE_FORMAT` in `board_fetch.py` after changing what it writes; every live posting is rewritten on the next run.
- **Model thinking:** `model_thinking` in `pipeline_config.json`. Off (the default) is fast and repeatable. On is about 4× slower, varies from run to run, and can overflow the context. See README.
- **Score caps:** `score_caps` in `pipeline_config.json` enforces the prompt's hard limits after each pass, by employer, title, or posting text, and flags the rows it matches.
- **Shortlist crowding:** `shortlist_per_employer` in `pipeline_config.json`.
- **Cost control:** with a 30-day window, the data on disk on 2026-10-22 parsed to 353 unique listings, with 78 cut and 275 ranked (10 pass-1 batches plus 12 pass-2 batches, 9.4 min). A score cache would cut daily scoring to new listings (see Known Issues).

---

## Safety Guarantees

- **Read-only mail:** the jobs folder is opened with EXAMINE and fetched with `BODY.PEEK[]`. No flags change and nothing is deleted or expunged.
- **Verified TLS:** the IMAP server's certificate is checked before credentials are sent. Turning the check off is allowed only for a loopback host.
- **Closed scope:** stops if the configured jobs folder is missing rather than scanning the inbox
- **Compliance:**
  - Digest data comes only from emails the job site sends to this mailbox.
  - Board data comes only from the platforms' public posting APIs.
  - No HTML scraping.
- **Retention:**
  - Raw digests are kept in `raw/` for audit and replay.
  - Board postings that close are moved to `raw_boards/archive/`, not deleted.
- **Loud failures:** ingest and screening exit non-zero on failure (screening exits 2 when nothing was scored). A shortlist is never written from a run where nothing was scored.
- **Secrets and personal data:** credentials are read only from environment variables, and every personal setting is in gitignored files. Only the placeholder `.example` files are committed.

---

## Next Milestones

Agreed 2026-10-22, in this order:

1. **Collect verdicts for a few days.** Fill in `feedback/<date>.md` each morning. Today's verdicts shaped today's changes, so only later days' verdicts can judge the next change, or the looser absolute-rubric scores, out of sample.
2. **Tests for what the score cache will rely on:** listing identity, the cache key, and score-cap application. A stale or mis-keyed cache fails silently by serving old scores. This needs config loading moved out of import time first.
3. **Score cache plus per-listing scoring, built together:**
   - A cache filled from batches of 30 would freeze the ~6-point batch-mate effect into every cached score.
   - Per-listing scoring without a cache would mean ~270 requests every morning.
   - Together, the first run scores everything once, one request per listing. After that, a day scores only its new listings.
   - The cache key includes the model and a prompt hash, so a prompt or model change re-scores everything. Include a switch to force a full re-score.
   - Record `model` and the prompt hash in `ranked_<date>.json`.
   - Re-score every board posting against its text, not just the top `rerank_top_n`.
   - Measure per-listing request time before building. The estimate is a few seconds each: ~15–30 min for the first full run, ~1–2 min daily after that.
4. **Judge it** with `python verdicts.py`, and check day-to-day stability: a listing's score shouldn't move unless its listing, the prompt, or the model changed.

After that, as needed:
- **Separate board and digest sections** in the shortlist, only if the verdicts show digest "yes" rows below the cut.
- **Visibility for Ollama outages:** a preflight check and a `FAILED_<date>.md` marker.
- **The two scheduling misses** (10-14, 10-22).
- **Parser canary** for digest format drift.
- **UIDVALIDITY tracking.**

Success metric for automation: zero manual intervention; shortlist present in `out/` by 7:00 AM each weekday.

*Last updated: October 22, 2026*
