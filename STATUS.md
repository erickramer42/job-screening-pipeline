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
| LLM ranking | ✅ Operational | Pass 1 scores every survivor 0–100 from title/employer/location. Pass 2 re-scores the top `rerank_top_n` listings that have a description, using a role-section excerpt. Thinking is off and the first attempt is greedy, so scores are repeatable; `score_caps` (by employer, title or posting text) are enforced after each pass. Malformed responses are retried; 3 consecutive failed batches stop a pass. |
| Output generation | ✅ Operational | Produces `out/shortlist_YYYY-MM-DD.md` and JSON. Board rows include direct posting links and their posted date; ▲ marks pass-2 scores. The JSON records the model, prompt and settings behind the run. |
| Scheduled runs | ✅ Operational | Task Scheduler runs ingest at 01:00 and 05:00, and board fetch + screening at 06:00. Tasks wake the computer, run after a missed start, and the wrappers retry (see [Automation](#automation)). Verified across 2026-10-09 to 10-23, with two misses. On 10-14 no task ran: a Windows Update restart at 00:33 left no one signed in until 08:52, and the missed-start catch-up didn't run the tasks after sign-in. On 10-22 screening failed all 3 attempts because Ollama wasn't running. Since 2026-10-23 both tasks also start at log on, the screening wrapper starts Ollama before a retry, and a failed morning leaves `out/FAILED_<date>.md`. |

### Recent Changes (2026-10-23)
- **Board fetch, from the review of the public-boards work:**
  - **Multi-office locations are short and stable:** a Greenhouse location built from offices names the in-area ones first, then the rest by name, at most three, and counts the others (`; +4 more`). So the line no longer grows with every office or changes when the API reorders them. Which postings pass the filter is unchanged.
  - **Remote-labeled offices** ("Remote <country>") are left out of a Hybrid or In-Office location, so a "Remote US" office can't pass a hybrid job as US remote. The 26 such offices on the live boards today are all outside the US, on postings the filter drops anyway.
  - **A SmartRecruiters re-release** (only the published date changed) rewrites the file but keeps its description, instead of costing a detail request.
  - **Pay:** Lever and Ashby share one pay reader. A Lever range whose top is 0 is read as its minimum, as Ashby's already was, instead of being cut by the pay floor. A malformed Lever `salaryRange`, Ashby pay tier or Greenhouse office no longer fails the whole board.
  - **Lever headings** that already hold an entity (`&amp;`) are no longer escaped twice.
  - **JSON:** `posted` is set only on rows with a date; digest rows no longer carry `"posted": ""`.
  - **Docs:** a Lever row's Posted date is the creation date, and a "Distributed" posting is kept only when an office names a metro city or the US.
  - **Tests:** 143, up from 141. The board-file contract test checks the Posted date too, and the screener tests build board files with board fetch's own `record_text`.
  - **Checked** against the live APIs, on copies of `raw_boards/`:
    - The committed and new board fetch gave the same counts on every board. 258 of 281 files were identical; the other 23 differed only in the location line, which went from at most 264 characters to 124 and names a subset of the old offices.
    - A second run of the new code rewrote nothing.
    - With a stand-in model, the screener sent the same 25 requests, with 22 pass-1 lines changed, in their location only.
    - The next scheduled run rewrites those files once, with the format-3 rewrite already due, so the 10-24 → 10-25 stability baseline isn't affected.
- **Board postings whose location is only "Hybrid", "In-Office" or "Distributed" are no longer lost:**
  - One configured Greenhouse board puts the work arrangement in the location field and the city only in the posting's offices. None of its 216 title-matching postings passed the area filter, so the board counted as having "none in the area".
  - For such a location, board fetch now adds the office locations (`Hybrid: <city>`), and "Distributed" counts as remote. Every other location is unchanged.
  - On the live boards, 101 postings from that board are now kept. No other board's postings changed. The daily run gains three pass-1 batches, about a minute.
- **Lever postings carry their description and pay:** the description is built from the body, the bulleted lists under their headings, and the closing text. Pay comes from `salaryRange`, in USD per year or per hour, so `pay_floor` applies. The schema was checked on live Lever boards. No Lever board is configured yet.
- **The shortlist shows when each board posting was published** (Posted column, and `posted` in the JSON). Board postings are exempt from `recency_days`, so a posting open for months used to look like a new one. Digest rows show `-`.
- **Board file format 3:** a `Posted:` line, and no `Title:` or `Pay:` lines, which nothing read (review L11). Every board file is rewritten once by the next run, and the SmartRecruiters descriptions are fetched once more.
- **Tests:** 141, up from 133.
- **Checked:** against the live APIs, the committed and new board fetch ran on copies of `raw_boards/`.
  - All 181 existing files kept identical listing lines and descriptions. Only their metadata changed, as described.
  - The 101 new files all come from that one board. Nothing more was archived.
  - With a stand-in model, the screener sent all 272 earlier pass-1 lines unchanged, plus 99 new ones; 2 of the new postings were duplicates or hard-filtered.
- **Scheduled runs recover on their own:**
  - **An update restart no longer costs a morning:** both tasks also start at log on, ingest after 5 minutes and screening after 15. Screening skips a start before 06:00, since the daily run comes then, and a day whose shortlist is already written.
  - **Ollama down:** before each retry, the screening wrapper starts the Ollama app if it isn't running. Ollama already starts at login, but was down at 06:00 on 10-22.
  - **A failed morning shows in `out/`:** after the last failed attempt, the wrapper writes `out/FAILED_<date>.md` with what the exit code means and the log's error lines. A later run that day removes it.
  - The date logic is in the new `wrapper_helper.py`, since `cmd`'s `%date%` and `%time%` follow the locale.
  - The wrappers' logic is versioned: `run_ingest.bat.example` and `run_screen.bat.example` are committed copies.
  - **Checked:** a copy of the wrapper ran five scenarios with stub scripts. Both tasks kept their daily triggers and settings; their old definitions are saved in `logs/`.
- **A same-day rerun updates `feedback/<date>.md`:** while it's still the untouched template, it's rewritten for the rerun's rows. Once anything is filled in, only the rows new to the list are appended, under a comment, and nothing already there changes.
- **The ranked JSON:**
  - It records what produced the run: the model, fingerprints of the prompt, the pass-2 addendum and the `score_caps` rules, and the settings that affect scores. So the 10-24 and 10-25 runs, the stability baseline, can show they used one prompt.
  - It no longer copies each posting's text, which stays in the row's source file. A re-scored row keeps the excerpt pass 2 sent instead. 1.35 MB → 0.22 MB on 10-23 data.
- **Ingest searches with SINCE**, so the server leaves digests older than `recency_days` out, instead of a header fetch per message. Checked read-only against the mailbox: with a day of margin it missed no message at windows of 1 to 10 days.
- **City names in `metro_cities` are plain text, matched as whole words.** They used to be joined into a regular expression. No match changed among 837 locations on disk and on the live boards.
- **`slug_probe.py`** prints what a Greenhouse or SmartRecruiters board calls itself next to each VERIFIED line, and CHECK when the name differs. No configured board differed.
- **`boards.json`:** the board that returned no postings was removed. No supported platform has postings for that company.
- **Checked, and closed without a change:** remote-by-timezone locations (none among 1,773 title-passing postings), near-duplicate variants on the shortlist (mostly different roles; a title rule would merge different jobs), and cross-source dedup (the sources still don't overlap). The dedup key ignoring location affects 1 of 360 listings, so it's now Low.
- **Tests:** 133, up from 116.
- **Checked:** on today's data with a stand-in model, the committed and new screener sent the same 22 requests and wrote an identical shortlist and verdict template, and the JSON changed only as described. Board fetch left identical folders against the live APIs.
- **Exit codes pinned before the scoring rewrite:**
  - Tests now run the screener end to end with a stand-in model, one for each exit code the wrapper relies on.
  - **A failed or skipped pass 2 now exits 1.** It used to exit 0, with only a note in the shortlist, so it never reached `errors.log`. The wrapper still doesn't retry it.
  - **A request Ollama rejects** (an HTTP 4xx, such as a model that can't think while `model_thinking` is on) stops the run at once with exit 1, and `screen.log` shows Ollama's reason. Before, it failed every batch and exited 2, so the wrapper retried it three times, 15 minutes apart. Server errors (5xx) still count as an outage, now with their reason logged too.
  - A retry whose connection fails keeps the scores of the attempt before it, instead of losing the whole batch.
- **Applied jobs and verdicts follow board postings by URL:**
  - A feedback file's run holds each board row's posting URL. So a job marked applied stays cut, and its verdict still counts, after the employer edits the title.
  - Verdict files are read as written.
  - On the real files, all 8 applied jobs got a URL, and so did every board verdict.
- **`verdicts.py`:**
  - A later line updates a verdict instead of replacing it. `[a]` alone keeps the earlier y/m/n, an empty note keeps the earlier note, and "applied" stays set.
  - Rows on the shortlist show their position on it. A row the per-employer limit held out says so.
- **Digest files carry UIDVALIDITY:**
  - Ingest writes `raw/job_uv<UIDVALIDITY>_uid<UID>.txt`. If the server resets UIDVALIDITY, the window is ingested again, instead of new digests being skipped as duplicates.
  - Ingest stops if the server doesn't report UIDVALIDITY.
  - The next ingest renames the 38 existing files once. Checked first, read-only against the mailbox: all 38 match the server's current UIDs.
- **Parser canary:** a recent digest with text but no listings gets a WARNING in the shortlist, as the sign of a changed digest format. All digests on disk have listings today.
- **Board fetch:**
  - Each board's log line starts with how many postings the API returned. One board returns none at all, which suggests a stale slug. Three others return postings, but none in the area. (For one of the three, that was the location format, fixed above.)
  - The files of a board removed from `boards.json` are archived.
- **Smaller fixes:**
  - An `applied.txt` line that can never match is reported.
  - `slug_probe.py` stops when its file is missing, instead of probing other names.
  - The three scripts report config mistakes the same way, in one line naming `pipeline_config.json`. A JSON syntax error used to give a traceback in board fetch.
- **Tests:** 116, up from 87.
- **Checked:**
  - On today's data with a stand-in model, the committed and new screener sent the same 22 requests and wrote identical files.
  - Against the live APIs, board fetch left identical folders.
  - `verdicts.py` gave the same figures for the 10-22 and 10-23 runs.
- **Ranking prompt, from the 10-23 shortlist review:** Node.js no longer counts as part of the candidate's stack. The model had cited it as a core-stack match on three shortlist rows.
  - A required Node.js backend is now one gap. A full-stack role that otherwise fits stays in the 60–74 band, below 75+.
  - A backend role built mainly on Node.js is plausible but flawed. Node.js listed only as a plus doesn't lower a score.
  - **Measured** in a scratch run on the same input:
    - The three rows fell from 78–88 to 62–68 and stayed on the shortlist, and no reason credits Node.js as the candidate's skill any more.
    - A first wording called the requirement a "stretch". It pushed two "yes" rows to 54–55 and off the list, because the prompt uses that word for its 30–54 band.
    - Ordering agreement with the 10-23 verdicts went from 64% to 61%, and "no" rows on the list from 11 to 7. Both moves are within the noise of re-batching pass 2: listings that don't mention Node.js moved 8.5 points on average there, against 2.3 in pass 1.
- **First out-of-sample verdicts:**
  - The 10-23 shortlist was the first scheduled run with the 10-22 changes. It drew 3 "yes", 10 "maybe", 11 "no" and 1 applied.
  - Ordering agreement was 64%, against 84% in-sample on the 10-22 verdicts.
  - Most "no" notes name a requirement in the posting text: years of a specialist tool, a required framework, or a niche domain.
- **Board files follow the posting:**
  - Board fetch used to write a posting's file once and rewrite it only after a `FILE_FORMAT` bump. On 10-23, 40 of 182 live files no longer matched their posting: 8 titles, 3 locations, 2 pay ranges and 32 descriptions had changed since Oct 10.
  - Now a file is rewritten whenever its text would change. SmartRecruiters descriptions, which cost a request per posting, are fetched again only when the listing lines change.
  - Files are written through a temporary file, so an interrupted run can't leave a truncated one.
  - **Checked** on a scratch copy against the live APIs: the first run rewrote exactly those 40 files (plus 1 new posting) and left the rest byte-identical; a second run rewrote nothing.
- **Tests:** `python -m unittest` runs a stdlib suite in `tests/`: deduplication, applied jobs, batch order, hard filters, score caps, the per-employer limit, and the verdict files. It needs no personal config.
- **Screener:**
  - Config, prompt and applied-list loading moved from import time into `main()`, so the module can be imported and tested without personal files.
  - Checked: with a stand-in model on today's data, the old and new code sent the same 22 requests and wrote byte-identical outputs.
  - A scratch run now points `OUT_DIR` and the new `TEMPLATE_DIR` at a scratch folder, and still reads the `a` verdicts from `feedback/`.
  - A missing required config key, a `score_caps` rule with no matcher or no `max_score`, and an invalid `excluded_title_patterns` regex now stop the run at startup with a message naming the problem.
  - Listing identity is defined once, in `verdicts.py`. An `applied.txt` line whose title contains a pipe used to be split at the first pipe, so that listing was never cut; it's now split like a verdict line, at the last one.
  - The per-employer limit's "More from" note groups employers regardless of case, as the limit already did.
- **`verdicts.py`:** ordering agreement compares scores, with equal scores counting half. It used to compare list positions, which break score ties in an arbitrary order. On the same verdicts, the 10-23 run's figure moved by 1 point.
- **No script reads files at import any more:**
  - Board fetch and ingest load config in `main()`, like the screener. Settings errors stop the run with the same messages and exit codes as before.
  - The new `pipeline_common.py` holds config loading, the description marker, email date parsing, and atomic writes.
- **Atomic writes everywhere:** digests, the ranked JSON, the shortlist and the verdict template are written through a temporary file, as board files already were. A killed run used to leave a truncated digest that was skipped forever as a duplicate.
- **Ingest:**
  - Email bodies are decoded with the charset they declare, and the two bare `except:` clauses are gone.
  - Checked against all 38 messages in the folder, read-only: the new code reproduces every digest file on disk byte for byte, apart from the `Job_ID` line retired on 10-22. All 38 are single-part UTF-8 HTML, so nothing changes today.
  - A message with no text part is saved without listings, so it isn't downloaded again on every run.
- **Ollama preflight:**
  - Before scoring, the screener checks Ollama. If it can't be reached, the run exits 2 and writes nothing; the wrapper retries that, as before.
  - If Ollama is running without the model, the run exits 1 at once, instead of three attempts 15 minutes apart.
- **Config:** pass 1's batch size (`pass1_batch`, 30) and the context window (`num_ctx`, 8192) moved from code into config, with the same defaults.
- **JSON:** a re-scored row that was capped in pass 1 keeps the model's own pass-1 score as `pass1_capped_from`.
- **`slug_probe.py`:** a network failure is reported as a failed lookup, and the script exits 1. It used to report "no board found" for every company. The `boards.json` lines it prints are now valid JSON even for a name with quotes.
- **Tests:** 87 now, up from 26 at the start of the day. Added: model-response handling, the pass-2 excerpt, the board-file layout and rewrite rules, email decoding, the slug probe, combined verdict flags, and the contract that what ingest and board fetch write parses in the screener.
- **Checked:**
  - The committed and new code ran on the same data, the screener with a stand-in model and board fetch against the live APIs.
  - The screener sent the same 22 requests and wrote identical outputs, apart from the new field. Board fetch left identical board folders.
- **The 10-14 scheduling miss, explained:**
  - Windows restarted for an update at 00:31–00:33, and no one signed in until 08:52.
  - The tasks run only while the user is signed in, so the 01:00, 05:00 and 06:00 starts couldn't happen, and the catch-up didn't run them after sign-in. That day's files came from manual runs.
  - Fixing it means changing Windows or Task Scheduler settings. Done later on 10-23: both tasks also start at log on.
- **Verdict flags combine:**
  - A feedback row can now say `[m,a]`: a verdict plus "applied". `[a]` alone still works.
  - The old parser expected a single letter, so it silently skipped the first combined rows. The 7 jobs applied to on 10-23 would have reappeared on the next shortlist.
  - A row whose flags can't be read is now reported with its file and line number, instead of being skipped.
  - `verdicts.py` leaves a listing cut as applied out of its figures. A "yes" you applied to isn't counted as ranked last.
- **`.gitignore`** also covers `pipeline_config.before-*.json`, the backups of the personal config taken before a rule change.
- **Docs:** README.md is now a short project overview. Its setup and operating guide moved to `docs/USAGE.md`, and its settings reference to `docs/CONFIGURATION.md`.
- **Tuning notes from the 10-23 review:**
  - **Ranking prompt:** the profile's core is the backend stack. TypeScript/React is secondary, and the technologies the candidate lacks are named.
    - A posting that requires one of those technologies is a real gap, and so is a front-end-led role.
    - A full-stack role with a Node.js backend that otherwise fits stays in 60–74.
  - **`score_caps`:** five new rules cap a required skill the candidate lacks: Spark/Databricks, Rails, C#/.NET and production AWS at 50, Node.js at 65.
    - A rule matches a posting line that names the skill with years or strong wording, doesn't call it optional, and isn't a list of alternatives. [docs/CONFIGURATION.md](docs/CONFIGURATION.md#score-caps) shows the pattern.
    - On 10-23 data they caught 8 of the 11 "no" rows on the shortlist, and no "maybe" row.
  - **Two flags, no cap:** `fde` for Forward Deployed titles, and `perm` for labor-certification-style requirements ("at least N years of prior work experience in each of the following").
  - **"Re-check postings are still open":** already done for board rows, since board fetch runs right before screening and archives closed postings. The posting that closed on 10-23 was still open at 06:00 and closed later that day. Digest rows have no link to check.
  - **Measured** in two scratch runs on the same input (in-sample, since these verdicts shaped the change):
    - ordering agreement 57% → 77%;
    - "no" rows on the shortlist 11 of 21 → 5 of 21, and "yes" 0 of 2 → 1 of 2, with "maybe" unchanged at 6 of 10;
    - digest rows on the shortlist 7 → 10;
    - pass-1 scores of listings the change doesn't touch moved 2.3 points on average.

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
| Pass-2 scores share a ranking with pass-1 scores | Medium | Only listings with a description (board rows) are re-scored against posting text, so board rows can crowd digest rows out of the top 25. Much smaller since 2026-10-22: the 10-23 scheduled run's top 25 had 18 board and 7 digest rows, and the verdicts so far show no digest "yes" row below the cut. | Separate shortlist sections, only if `verdicts.py` shows digest "yes" rows below the cut |
| Every run re-scores everything in the recency window | Medium | Each day re-scores all digest listings from the last `recency_days` plus every live board posting, though ~97% were scored the day before. The 10-23 scheduled run took 4 min 41 s; runs took ~25 min before 2026-10-22. | Score cache keyed by listing |
| Scores depend on batch-mates | Medium | The same listing scored in a different batch of 30 moves ~6 points on average (8% move 20+), against ~1.2 for a repeat of the same batch. The per-batch quota is gone (2026-10-22), but the model still grades partly against the other listings in its request. | Score each new listing in its own request, together with the score cache |
| The ranking prompt's profile treats C++ as core stack | Medium | The profile was a flat skills list, so the model credited C++ (about 2 professional years, nearly a decade ago, plus side projects) as a core match: 5 of the 10-24 run's reasons cite it, and C++-centered rows scored up to 78. | A profile tiered by recency and weight, measured 2026-10-24 and parked as `screening_prompt.recency-pending.txt`. It goes live after the 10-25 scheduled run, so the 10-24 → 10-25 stability baseline keeps one prompt. In two scratch runs on 10-24 input it took C++-centered rows to 30–35 and left no reason citing C++ as his skill; ordering agreement 73% against 72% (in-sample), and 5 "no" rows on the shortlist against 9. Known miss: a "yes" posting that accepts Python or C++ fell from 65 to 45, outside pass 2's top `rerank_top_n`, so its text was never read; re-scoring every board posting (milestone 3) fixes that. And a "no" posting in a specialist domain rose from 54 to 78 once the freed pass-2 slots let it in. |
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
├── html_text.py                 # Shared HTML-to-text conversion (ingest + boards)
├── location_filter.py           # Shared search-area rule (boards + digest filter)
├── pipeline_common.py           # Shared config loading, description marker,
│                                #   email dates, atomic writes
├── verdicts.py                  # Reads shortlist verdicts; scores a run against them
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
│   └── CONFIGURATION.md         # Settings reference
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
- Digest rows need the original email for their "View" links.

Apply manually at company career pages. Afterwards:
- Fill in `feedback/<date>.md`: a y/m/n verdict per row, with notes after `::`. Add `a` when you've applied (`[m,a]`, or `[a]` alone); it cuts the job from future rankings.
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
2. Ollama service must be running — set Ollama to launch at login or install as a service. The screening wrapper also starts the Ollama app before a retry, from its default install folder.
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
- **Pass 2:** `rerank_top_n` (`0` disables), `rerank_batch`, and `excerpt_chars` in `pipeline_config.json`. Excerpt selection uses `ROLE_MARKERS` and `FOOTER_MARKERS` in `jobs_screen.py`.
- **Batch size:** lower `pass1_batch` (30) in `pipeline_config.json` to 20 or 15 if pass-1 responses come back truncated
- **Model choice:** swap `SCREEN_MODEL` for a smaller model (speed) or larger one (quality)
- **Board file format:** bump `FILE_FORMAT` in `board_fetch.py` after changing what it writes; every live posting is rewritten on the next run.
- **Model thinking:** `model_thinking` in `pipeline_config.json`. Off (the default) is fast and repeatable. On is about 4× slower, varies from run to run, and can overflow the context. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#model-thinking).
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
2. **Tests for what the score cache will rely on:** listing identity, the cache key, and score-cap application. A stale or mis-keyed cache fails silently by serving old scores. Done 2026-10-23 for listing identity and score caps, with config loading moved out of import time; the cache-key tests come with the cache. Board files now follow edits to their posting, which the cache's keys depend on.
3. **Score cache plus per-listing scoring, built together:**
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
- **Separate board and digest sections** in the shortlist, only if the verdicts show digest "yes" rows below the cut.
- **Ollama structured outputs** (a JSON schema as the response format): a scoring change, so it waits for verdicts like any other.

The automation items (the 10-14 and 10-22 misses, a visible failure marker) and the empty board were done on 2026-10-23.

Success metric for automation: zero manual intervention; shortlist present in `out/` by 7:00 AM each weekday.

*Last updated: October 23, 2026*
