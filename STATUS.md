# STATUS.md — Job Screening Pipeline

## Overview

This project automates discovery, triage, and ranking of job postings from two sources:

1. **Job-alert digest emails**: weekly saved-search emails from a job site, read from the candidate's mailbox.
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
| Board fetch (`board_fetch.py`) | ✅ Operational | Pulls `boards.json` companies into `raw_boards/` and filters by title, metro-area/US-remote location, and pay floor. Closed postings move to `raw_boards/archive/`. |
| Slug discovery (`slug_probe.py`) | ✅ Operational | Guesses board slugs for company names (e.g. `targets.txt`) and prints `boards.json` lines for slugs that return jobs |
| Job parsing (`jobs_screen.py` parser) | ✅ Operational | Reads title/employer/location quadruples from `raw/` and `raw_boards/` |
| Hard filters | ✅ Operational | Blocks the employers and title patterns listed in `pipeline_config.json` |
| LLM ranking | ✅ Operational | Uses Ollama (default model: `SCREEN_MODEL`) to score survivors 0–100. Stops after 3 consecutive batch failures. |
| Output generation | ✅ Operational | Produces `out/shortlist_YYYY-MM-DD.md` and JSON. Board rows include direct posting links. |
| Scheduled runs | 🟡 Being verified | Task Scheduler runs ingest at 01:00 and 05:00, and board fetch + screening at 06:00. Tasks wake the computer, run after a missed start, and the wrappers retry (see [Automation](#automation)). First runs on 2026-10-09. |

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
  - No shortlist is written and it exits 1 if no batch scores (e.g. Ollama down).
  - Partial failures write the shortlist with a WARNING banner and `failed_batches` in the JSON, and exit 1.
  - The ranking prompt is loaded from the file at `prompt_path`.
  - Employer exclusions match whole words, so a short name can't cut an unrelated employer whose name contains it.
  - Exits 2 (instead of 1) when no batch could be scored. Nothing is written in that case, so the scheduled wrapper can retry safely.
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
| Every run re-parses and re-scores the full digest history | Medium | ~3,500 listings / ~118 batches daily, though digests arrive weekly. Months-old postings rank alongside new ones. | Score cache keyed by listing, plus a recency window using the Date header |
| LLM output not validated | Medium | A null `reason` crashes before the shortlist is written. A null index or score drops the rest of the batch. 1-based indices shift every score to the neighbouring job. | Verify the index set is exactly 0..n-1 and coerce types |
| Dedup key ignores location; digest rows read before board rows | Medium | Same title+employer in another city is dropped (35 pairs). A matching board row (with link and pay) loses to the digest copy. | Add location to the key, or prefer rows with a URL |
| Ranking prompt says "no salary" | Medium | Board rows carry a pay range in the location field that `screening_prompt.txt` tells the model doesn't exist | Mention pay-in-parentheses in the prompt |
| Bad `boards.json` entry crashes `board_fetch.py` | Medium | An unknown platform or missing slug raises `KeyError` outside the per-board `try`, stopping all boards | Validate each entry and skip it with a message |
| `board_fetch.py` exits 0 when fetches fail | Medium | A scheduled run can't tell "all boards down" from success. `run_screen.bat` logs a non-zero exit, but fetch failures never produce one. | Exit 1 when any board fetch fails |
| UIDVALIDITY not tracked | Medium | If the server resets UIDVALIDITY (e.g. a local mail server rebuilds its cache), UIDs restart at 1 and new digests are silently skipped as duplicates | Record UIDVALIDITY; warn or re-ingest when it changes |
| HTML fallback in `extract_job_text()` | Medium | `</td>` becomes a tab and is collapsed to a space, so an HTML-only digest parses to 0 jobs. The docstring was changed but the code wasn't. | Map `</td>` to `\n` |
| Silent parser failure on format change | Medium | Screener finds 0 jobs without warning | Canary check: count `View` lines per file |
| Per-batch "at most 2 ≥ 75" rule | Low | Scores depend on which batch a listing lands in, then get sorted globally | Absolute rubric, or a re-rank pass over the top N |
| `slug_probe.py` "VERIFIED" only means the slug has jobs | Low | A generic name (e.g. "Google") can match a different company's board | Check the company on the board before adding to `boards.json` |
| Remote-by-timezone locations dropped | Low | e.g. Greenhouse "Eastern or Central Timezone" fails the location filter | Add timezone phrases to the remote check if needed |
| Existing `raw_boards/` files keep their old format | Low | Files written before the location fix lack the "(Remote)" tag | Delete `raw_boards/*.txt` once; the next fetch regenerates them |

---

## File Map

```
jobScreensApplications/
├── job_ingest.py                # IMAP fetcher, read-only,
│                                #   writes digests to raw/job_uid_<UID>.txt
├── board_fetch.py               # Public board APIs -> raw_boards/<platform>_<id>.txt
│                                #   Filters: title patterns/keywords, metro cities,
│                                #   remote/US check, pay floor
├── jobs_screen.py               # Parser + hard filters + LLM ranker -> out/
├── slug_probe.py                # Finds board slugs for company names
│                                #   (python slug_probe.py targets.txt)
├── pipeline_config.json         # All personal settings (gitignored)
├── pipeline_config.example.json # Placeholder config to copy
├── boards.json                  # Boards to fetch (gitignored)
├── boards.example.json          # Placeholder board list to copy
├── screening_prompt.txt         # System prompt for LLM ranking (gitignored; prompt_path)
├── targets.txt                  # Company names to probe (gitignored)
├── run_ingest.bat               # Task Scheduler wrappers (gitignored)
├── run_screen.bat
├── README.md                    # Setup, usage, and config reference
├── STATUS.md                    # This file: project state and roadmap
├── raw/                         # Ingested email digests (gitignored)
├── raw_boards/                  # One file per live board posting (gitignored)
│   └── archive/                 #   Closed/filtered-out postings; not screened
├── out/                         # Ranked shortlists, JSON + Markdown (gitignored)
├── processed/                   # Created by ingest; unused so far (gitignored)
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

Apply manually at company career pages.

Adding boards: put company names in `targets.txt` and run `python slug_probe.py targets.txt`. Check that each VERIFIED board really belongs to that company, then paste the lines into `boards.json`.

---

## Automation

Digest emails arrive **weekly**. Across 23 digests (Apr 20 – Oct 6, 2026), they arrived late Monday night, usually between 23:00 and 01:00 local time, with one as late as 01:35. Board postings change continuously, so a daily fetch is still worthwhile.

Goal: the pipeline runs unattended overnight and the shortlist is waiting in the morning.

### Schedule

| Time | Task | Wrapper | Runs |
|------|------|---------|------|
| 01:00 and 05:00 daily | `JobIngest` | `run_ingest.bat` | `job_ingest.py` |
| 06:00 daily | `JobScreen` | `run_screen.bat` | `board_fetch.py`, then `jobs_screen.py` |

The 01:00 ingest catches most digests (22 of the 23 observed). The 05:00 run picks up any that arrive later; the latest observed was 01:35. Emails already saved to `raw/` are skipped, so the second run costs almost nothing. Board fetch runs just before screening, so the shortlist uses the freshest postings.

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
- **Time limit:** 2 h for ingest, 8 h for screening. A full re-score of ~3,500 listings takes about 2.5 h at ~75 s per batch. The limit stops a hung run before the next day's run, which would otherwise be skipped ("do not start a new instance").
- **Runs only while the user is logged on.** This is deliberate: the local mail server and Ollama run in the user's session, so a logged-off run couldn't reach them. With catch-up on, a missed run starts once the user logs on.

### Prerequisites for unattended runs
1. The IMAP server must be reachable; if it runs locally, set it to auto-start with Windows
2. Ollama service must be running — set Ollama to launch at login or install as a service
3. `EMAIL_USER` and `EMAIL_PASSWORD` must be set as Windows user environment variables (see below)
4. Check Task Scheduler's **Last Run Result** and `logs/` after the first automated nights:
   - `logs/errors.log` gets one timestamped line per failed step (`ingest FAILED`, `board fetch FAILED`, `screen FAILED`), after retries.
   - The scripts exit non-zero on failure (except `board_fetch.py` fetch errors — known issue).
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
- **Ranking prompt:** edit the file at `prompt_path` (default `screening_prompt.txt`)
- **Batch size:** lower `BATCH = 30` to 20 or 15 if JSON parse failures appear
- **Model choice:** swap `SCREEN_MODEL` for a smaller model (speed) or larger one (quality)
- **Cost control:** the full corpus is ~3,500 email listings (~118 batches) plus ~90 board postings. For steady state, plan a score cache or recency window (see Known Issues) and a deterministic pre-rank cap (~top 200–300) before LLM scoring.

---

## Safety Guarantees

- **Read-only mail:** the jobs folder is opened with EXAMINE and fetched with `BODY.PEEK[]`. No flags change and nothing is deleted or expunged.
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

1. Verify 3–5 unattended overnight cycles (01:00 + 05:00 ingest, 06:00 screening)
2. Score cache + recency window so daily runs only score new listings
3. Validate LLM output (index set, null fields)
4. Parser canary warning on format drift
5. `board_fetch.py` robustness: skip bad `boards.json` entries; exit non-zero on fetch failures

Success metric for automation: zero manual intervention; shortlist present in `out/` by 7:00 AM each weekday.

*Last updated: October 9, 2026*
