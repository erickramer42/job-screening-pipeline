# STATUS.md — Job Screening Pipeline

## Overview

This project automates discovery, triage, and ranking of job postings from two sources:

1. **Job-alert digest emails**: saved-search emails from a job site, nightly since mid-October 2026, read from the candidate's mailbox.
2. **Company career boards**: public job-board APIs (Greenhouse, Lever, Ashby, SmartRecruiters) for the companies listed in `boards.json`.

The goal is a focused daily application routine (~10 highest-quality matches per day). Applications are not submitted automatically.

[README.md](README.md) is the project overview. Setup and operation are in [docs/USAGE.md](docs/USAGE.md), and the settings reference is in [docs/CONFIGURATION.md](docs/CONFIGURATION.md). This file tracks project state and planned work.

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
| Email ingest (`job_ingest.py`) | ✅ Operational | Fetches digest emails over IMAP into `raw/job_uv<UIDVALIDITY>_uid<UID>.txt`. Read-only (EXAMINE + `BODY.PEEK[]`). |
| Board fetch (`board_fetch.py`) | ✅ Operational | Pulls `boards.json` companies into `raw_boards/` and filters by title, metro-area/US-remote location, and pay floor. Stores each posting's description and published date. A Greenhouse location that names only the work arrangement is read with the posting's offices. Closed postings, and those of boards removed from `boards.json`, move to `raw_boards/archive/`. A posting's file is rewritten whenever the posting changes or the file format is older. |
| Slug discovery (`slug_probe.py`) | ✅ Operational | Guesses board slugs for company names (e.g. `targets.txt`) and prints `boards.json` lines for slugs that return jobs |
| Job parsing (`jobs_screen.py` parser) | ✅ Operational | Reads title/employer/location quadruples from `raw/` and `raw_boards/`. Keeps one copy per listing (board copy first, then newest digest) and cuts digests older than `recency_days`. A recent digest with text but no listings gets a warning in the shortlist. |
| Hard filters | ✅ Operational | Cuts the employers and title patterns listed in `pipeline_config.json`, digest listings outside the search area, and jobs already applied to (`applied.txt`, or an `a` flag in `feedback/`, which follows a board posting by URL) |
| LLM ranking | ✅ Operational | Pass 1 scores every survivor 0–100 from title/employer/location, in batches of 30. Pass 2 re-scores the top `rerank_top_n` listings that have a description, using a role-section excerpt; with `per_listing_rerank` (built 2026-10-26, not yet switched on) it re-scores every one, alone and through a score cache. Thinking is off and the first attempt is greedy, so scores are repeatable; `score_caps` (by employer, title or posting text) are enforced after each pass. Malformed responses are retried; 3 consecutive failed requests stop a pass. Each pass logs its peak context use. |
| Output generation | ✅ Operational | Produces `out/shortlist_YYYY-MM-DD.md` and JSON. The shortlist has two sections, each ranked on its own: listings pass 2 read against their posting text, and a "Needs lookup" section for those with a title-only score. Board rows include direct posting links and their posted date; ▲ marks pass-2 scores. A listing judged "no" is held at 50 on later days, below the listings not yet judged, and isn't put in the verdict template again. The JSON records the model, prompt and settings behind the run. |
| Scheduled runs | ✅ Operational | Task Scheduler runs ingest at 01:00 and 05:00, and board fetch + screening at 06:00. Tasks wake the computer, run after a missed start, and the wrappers retry (see [Automation](#automation)). Verified across 2026-10-09 to 10-26, with two misses, both before the fixes below. On 10-14 no task ran: a Windows Update restart at 00:33 left no one signed in until 08:52, and the missed-start catch-up didn't run the tasks after sign-in. On 10-22 screening failed all 3 attempts because Ollama wasn't running. Since 2026-10-23 both tasks also start at log on, the screening wrapper starts Ollama before a retry, and a failed morning leaves `out/FAILED_<date>.md`. |

### Recent changes

The full history, with the measurements behind each change, is in [docs/CHANGELOG.md](docs/CHANGELOG.md). The last few days:
- **2026-10-26:** pass 2 can re-score every posting in its own request, through a score cache (`per_listing_rerank`, built and off until it's switched on). Each pass logs how much of the model's context it used. A whole-project review fixed seven smaller problems, among them a verdict that could be dropped without a warning, and split the screener's model client and output writers into modules of their own.
- **2026-10-25:** the first pair of daily runs under one prompt gave the stability baseline. The ranking prompt's candidate profile is now weighted by how recent each skill is.
- **2026-10-24:** listings ranked on their title alone get their own "Needs lookup" section, and a "no" verdict holds a listing down on later days.
- **2026-10-23:** board files follow edits to their posting; the scheduled runs recover from a restart or a stopped Ollama on their own; the test suite and end-to-end exit-code tests.
- **2026-10-22:** verified TLS for mail, repeatable scoring (thinking off, greedy first attempt), score caps enforced in code, and the verdict loop.

### Pending / Known Issues
| Issue | Priority | Impact | Planned Fix |
|-------|----------|--------|-------------|
| Pass-2 scores move with their batch-mates, and every run re-scores them | Medium | A re-scored listing moves ~9 points from day to day with nothing changed (10-24 → 10-25). Pass 1 repeats exactly on identical input, but moves ~2.7 points when other listings come and go. | `per_listing_rerank`, built 2026-10-26 and off until switched on: pass-2 scores then come from the cache and don't move. Pass 1 stays batched ([changelog, 2026-10-26](docs/CHANGELOG.md#2026-10-26)) |
| Pass 2 reads only the top `rerank_top_n` description rows | Medium | Listings already judged "no" still take pass-2 slots, though they'll be held at 50: any in the pass-1 top 60 costs a slot a "yes" just below the cut could have had. Only re-scored rows fill the main section, so it's only as wide as the employers among them: 12 of 15 rows on 10-24 data, and on 10-25 data 8 of 15 were listings already judged "no". | `per_listing_rerank` re-scores every posting: on 10-26 data, judged-"no" rows in the main section went from 6 to 4 |
| Mobile-heavy roles aren't discounted | Low | The prompt doesn't mention mobile work. With the recency profile, a mobile-heavy "no" scored 82 on 10-25 input. | A prompt change, measured on its own after the recency profile has a few days of verdicts |
| Dedup key ignores location | Low | Same title+employer in another city is dropped. The kept copy is the board row when there is one, otherwise the newest digest. On 10-23 this hid 1 of 360 listings: a board posting with two locations in the area. | Add location to the key where the location strings are comparable; it's also listing identity, so it would reshuffle batches and orphan verdicts |
| `slug_probe.py` "VERIFIED" only means the slug has jobs, on Ashby and Lever | Low | A generic name (e.g. "Acme") can match a different company's board. Greenhouse and SmartRecruiters boards now show their own name, and a different one is marked CHECK; Ashby and Lever don't name the company. | Open an Ashby or Lever board before adding it to `boards.json` |

---

## File Map

```
jobScreensApplications/
├── job_ingest.py                # IMAP fetcher, read-only, writes digests
│                                #   to raw/job_uv<UIDVALIDITY>_uid<UID>.txt
├── board_fetch.py               # Public board APIs -> raw_boards/<platform>_<id>.txt
│                                #   Filters: title patterns/keywords, metro cities,
│                                #   remote/US check, pay floor
├── jobs_screen.py               # Parser + recency/hard filters + two-pass
│                                #   LLM ranker -> out/
├── ollama_client.py             # Model requests; validates and repairs answers
├── shortlist.py                 # Writes the shortlist and the verdict template
├── html_text.py                 # Shared HTML-to-text conversion (ingest + boards)
├── location_filter.py           # Shared search-area rule (boards + digest filter)
├── pipeline_common.py           # Shared config loading, description marker,
│                                #   email dates, atomic writes
├── verdicts.py                  # Reads shortlist verdicts; scores a run against them
├── score_cache.py               # Per-listing pass-2 scores kept between runs
│                                #   (out/score_cache.json)
├── slug_probe.py                # Finds board slugs for company names
│                                #   (python slug_probe.py targets.txt)
├── wrapper_helper.py            # Date decisions for the wrappers: whether
│                                #   to screen now, and the failure marker
├── tests/                       # unittest suite (python -m unittest)
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
├── run_ingest.bat.example       # Committed copies of the wrappers, kept
├── run_screen.bat.example       #   identical (they hold no personal data)
├── README.md                    # Project overview
├── STATUS.md                    # This file: project state and roadmap
├── docs/
│   ├── USAGE.md                 # Setup, running, outputs, review loop
│   ├── CONFIGURATION.md         # Settings reference
│   └── CHANGELOG.md             # What changed, day by day, with measurements
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

Review `out/shortlist_<today>.md`. If it's missing, `out/FAILED_<today>.md` says why.
- Board rows have direct posting links.
- Rows under "Needs lookup" were scored on their title alone. Digest rows need the original email for their "View" links.

Apply manually at company career pages. Afterwards:
- Fill in `feedback/<date>.md`: a y/m/n verdict per row, with notes after `::`. Add `a` when you've applied (`[m,a]`, or `[a]` alone); it cuts the job from future rankings. An `n` holds the job at 50 on later shortlists.
- Add jobs applied to outside the shortlist to `applied.txt` (URL, or `Title | Employer` for digest rows).
- Run `python verdicts.py` after changing the prompt or config, to see whether the ranking moved toward your verdicts.
- Turn repeated notes into config rules or prompt changes (see [Reviewing the shortlist](docs/USAGE.md#reviewing-the-shortlist)).

Adding boards: put company names in `targets.txt` and run `python slug_probe.py targets.txt`. Check that each VERIFIED board really belongs to that company, then paste the lines into `boards.json`.

---

## Automation

Digest emails arrive **nightly**: usually three a night since 2026-10-11 (35 from Oct 11 to 22), all between 23:59 and 00:35 local time. Before that, from Apr 20 to Oct 6, 2026, there were 23 weekly digests, arriving late Monday night between 23:00 and 01:35. Board postings change continuously, so a daily fetch is worthwhile too.

Goal: the pipeline runs unattended overnight and the shortlist is waiting in the morning.

### Schedule

| Time | Task | Wrapper | Runs |
|------|------|---------|------|
| 01:00 and 05:00 daily, and 5 min after log on | `JobIngest` | `run_ingest.bat` | `job_ingest.py` |
| 06:00 daily, and 15 min after log on | `JobScreen` | `run_screen.bat` | `board_fetch.py`, then `jobs_screen.py` |

The 01:00 ingest has caught every nightly digest since Oct 11, and 22 of the 23 weekly ones before that. The 05:00 run is insurance for late arrivals; it hasn't been needed since the nightly schedule started. Emails already saved to `raw/` are skipped, so the second run costs almost nothing. Board fetch runs just before screening, so the shortlist uses the freshest postings.

The log-on starts cover a night when an update restart leaves no one signed in, as on 10-14. A repeated ingest costs nothing. Screening runs at most once a day: a log-on start before 06:00 is skipped, since the daily run comes then, and so is one after today's shortlist was written.

### Wrappers

The wrappers are gitignored; `run_ingest.bat.example` and `run_screen.bat.example` are committed copies, kept identical. Both run under `cmd` (no Git Bash) and contain no machine-specific paths: each one changes to its own folder with `cd /d "%~dp0"`. `%date%` and `%time%` follow the locale, so the screening wrapper leaves date decisions to `wrapper_helper.py`.
- **Both:**
  - Create `logs\` if it's missing, and trim their own logs to the newest 2,000 lines before running.
  - Set `PYTHONUTF8=1`. Output redirected to a file would otherwise use the ANSI code page, and a character outside it crashes the script.
  - Run Python with `-u`, so log output is written as it happens and survives a run that gets killed.
  - Write a header line to the log for each attempt.
- **`run_ingest.bat`:** up to 3 attempts, 10 minutes apart, for a mail server that's still starting after wake or login. Output goes to `logs\ingest.log`. If every attempt fails, it writes `ingest FAILED rc=<code> attempts=3` to `logs\errors.log` and exits with the script's code.
- **`run_screen.bat`:**
  - First asks `wrapper_helper.py begin 06:00` whether to run. Exit 10 means skip: before 06:00, or today's shortlist was already written at or after 06:00. Any other answer, a crash included, runs. A run that starts removes the day's failure marker.
  - Runs board fetch, logging to `logs\boards.log`. A non-zero exit is logged to `errors.log`, and screening still runs on the postings already on disk.
  - Then runs the screener, logging to `logs\screen.log`. Only exit code 2 is retried (nothing scored, so nothing was written, usually because Ollama is still starting): up to 3 attempts, 15 minutes apart. Before each retry it starts the Ollama app, unless it's already running.
  - Partial failures (exit 1) aren't retried, because a retry would re-score everything. They're logged as `screen FAILED rc=<code> attempts=<n>`.
  - After the screener's last failed attempt, `wrapper_helper.py failed` writes `out/FAILED_<date>.md`: what the exit code means, and the last attempt's error lines.
  - Exits 1 if either step failed.

### Task settings (current)
- **Triggers:** daily at the times above, and at log on with a delay (5 min for ingest, 15 for screening; added 2026-10-23). **Action:** the wrapper, which sets its own working directory. The definitions from before the log-on triggers are saved as `logs/<task>.before-logon-trigger.xml`; `Register-ScheduledTask -Xml` restores one.
- **Wake the computer to run this task:** on. **Run as soon as possible after a missed start:** on, so a run missed while the PC was off or logged out happens at the next opportunity.
- **Can start on battery and keeps running on battery:** on.
- **Time limit:** 2 h for ingest, 8 h for screening. A full run on 2026-10-22 data (275 ranked listings, 22 batches) took 9.4 min, and the 10-23 scheduled run took 4 min 41 s. The limit stops a hung run before the next day's run, which would otherwise be skipped ("do not start a new instance").
- **Runs only while the user is logged on.** This is deliberate: the local mail server and Ollama run in the user's session, so a logged-off run couldn't reach them. Catch-up alone didn't run the tasks after the 10-14 sign-in, hence the log-on triggers.

### Prerequisites for unattended runs
1. The IMAP server must be reachable; if it runs locally, set it to auto-start with Windows
2. Ollama service must be running — set Ollama to launch at login or install as a service. The screening wrapper also starts the Ollama app before a retry, from its default install folder. Nothing else should be using Ollama at 06:00: another project's model swaps in and out with the screener's, and on 10-25 that made a 36-minute run take 3 h 20 min.
3. `EMAIL_USER` and `EMAIL_PASSWORD` must be set as Windows user environment variables (see below)
4. Check Task Scheduler's **Last Run Result** and `logs/` after the first automated nights:
   - `logs/errors.log` gets one timestamped line per failed step (`ingest FAILED`, `board fetch FAILED`, `screen FAILED`), after retries.
   - The scripts exit non-zero on failure. `board_fetch.py` tries every board first, then exits 1 if any failed.
   - **No shortlist for today** means scoring failed completely (Ollama down, model missing, or a request Ollama rejects) or a wrapper path/env-var problem. `out/FAILED_<date>.md` says which, with the log's error lines; `logs/screen.log` has the rest. A missing model or a rejected request fails on the first attempt (`screen FAILED rc=1`); Ollama being down fails after three (`rc=2 attempts=3`).
   - **A WARNING line at the top of the shortlist** means some pass-1 batches failed and those listings are ranked 0, or that a recent digest parsed to no listings.
   - **A NOTE line** means some pass-2 batches failed, so those rows kept their pass-1 scores. The run is logged as `screen FAILED rc=1`.

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
- **Pass 2:** `rerank_top_n` (`0` disables), `rerank_batch`, and `excerpt_chars` in `pipeline_config.json`; or `per_listing_rerank`, which re-scores every posting through the score cache (`score_cache_days`). Excerpt selection uses `ROLE_MARKERS` and `FOOTER_MARKERS` in `jobs_screen.py`.
- **Batch size:** lower `pass1_batch` (30) in `pipeline_config.json` to 20 or 15 if pass-1 responses come back truncated
- **Model choice:** swap `SCREEN_MODEL` for a smaller model (speed) or larger one (quality)
- **Board file format:** bump `FILE_FORMAT` in `board_fetch.py` after changing what it writes; every live posting is rewritten on the next run.
- **Model thinking:** `model_thinking` in `pipeline_config.json`. Off (the default) is fast and repeatable. On is about 4× slower, varies from run to run, and can overflow the context. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#model-thinking).
- **Score caps:** `score_caps` in `pipeline_config.json` enforces the prompt's hard limits after each pass, by employer, title, or posting text, and flags the rows it matches.
- **Shortlist crowding:** `shortlist_per_employer` in `pipeline_config.json`.
- **Cost control:** with a 30-day window, the data on disk on 2026-10-22 parsed to 353 unique listings, with 78 cut and 275 ranked (10 pass-1 batches plus 12 pass-2 batches, 9.4 min). On 10-26 data, a batch run took about 6 minutes; with `per_listing_rerank` and nothing changed, 4.5 minutes, nearly all of it pass 1.

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
2. **Tests for what the score cache will rely on:** listing identity, the cache key, and score-cap application. A stale or mis-keyed cache fails silently by serving old scores. Done 2026-10-23 for listing identity and score caps, with config loading moved out of import time; the cache-key tests come with the cache. Board files now follow edits to their posting, which the cache's keys depend on.
3. **Score cache plus per-listing scoring, built together.** Built 2026-10-26 for pass 2 only, the owner's choice after measuring both (see Recent Changes), and off until switched on. Switch it on (`per_listing_rerank: true`) once the recency-tiered prompt has a couple of days of verdicts on its own, around 10-27 or 10-28, so each change gets its own read. The plan as written:
   - A cache filled from batches of 30 would freeze the ~6-point batch-mate effect into every cached score.
   - Per-listing scoring without a cache would mean ~270 requests every morning.
   - Together, the first run scores everything once, one request per listing. After that, a day scores only its new listings.
   - The cache key includes the model and a prompt hash, so a prompt or model change re-scores everything. Include a switch to force a full re-score.
   - Record `model` and the prompt hash in `ranked_<date>.json`. Done 2026-10-23: the JSON's `run` record.
   - Re-score every board posting against its text, not just the top `rerank_top_n`.
   - Measure per-listing request time before building. The estimate is a few seconds each: ~15–30 min for the first full run, ~1–2 min daily after that. In the same session, check what Ollama's `prompt_eval_count` counts, so each pass can log how much of `num_ctx` it used.
   - Done 2026-10-23, before the rewrite: tests run the screener end to end with a stand-in model. They pin its exit codes and the rule that a run with nothing scored writes nothing, and the rewrite must keep them passing. A failed pass 2 now exits 1.
4. **Judge it** with `python verdicts.py`, and check day-to-day stability: a listing's score shouldn't move unless its listing, the prompt, or the model changed.

After that, as needed:
- **Ollama structured outputs** (a JSON schema as the response format): a scoring change, so it waits for verdicts like any other.

The automation items (the 10-14 and 10-22 misses, a visible failure marker) and the empty board were done on 2026-10-23. The separate section for listings without posting text was done on 2026-10-24.

Success metric for automation: zero manual intervention; shortlist present in `out/` by 7:00 AM each weekday.

*Last updated: October 26, 2026*
