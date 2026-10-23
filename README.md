# Job Screening Pipeline

Collects job listings from two sources, removes obvious mismatches with deterministic filters, ranks the rest with a local LLM, and writes a daily shortlist. It finds and ranks; you review the shortlist and apply yourself.

Sources:
- **Job-alert digest emails**: saved-search emails from a job site, read over IMAP from a dedicated mail folder. Each job in the email must appear as four lines: title, employer, location, and a `View` link.
- **Company career boards**: public posting APIs for Greenhouse, Lever, Ashby, and SmartRecruiters, for the companies you list.

Everything is plain Python standard library. There is nothing to `pip install`.

## How it works

```
job_ingest.py    mail folder (IMAP)     ->  raw/job_uid_<UID>.txt
board_fetch.py   boards.json (APIs)     ->  raw_boards/<platform>_<id>.txt (with description)
jobs_screen.py   raw/ + raw_boards/     ->  recency + hard filters -> pass 1 -> pass 2 -> out/
```

1. **Ingest** saves each digest email as a text file, skipping digests older than `recency_days` after checking only their Date header. Mail is opened read-only, so emails are never marked read, moved, or deleted.
2. **Board fetch** pulls each configured board and keeps postings whose titles match your patterns and whose location is in your metro area or remote within the US.
   - It drops postings whose published pay range tops out below your floor. Postings that close or stop passing the filters move to `raw_boards/archive/`.
   - Each posting's description is stored below the listing: from Greenhouse, Ashby and SmartRecruiters, but not yet Lever.
   - A posting's file is rewritten whenever its text would change: the employer edited the title, location, pay or description, or the file was written by an older version of the script. SmartRecruiters descriptions cost a request per posting, so they're fetched again only when the listing lines change. Files are written through a temporary file, so an interrupted run never leaves a truncated one.
3. **Screen** reads both folders and cuts excluded employers and title patterns. It also cuts digest listings outside your area, using the same metro-or-US-remote rule that board fetch applies to postings.
   - **Duplicates and staleness:** a listing that appears in several files is kept once. The board copy (with link, pay and description) is preferred, otherwise the newest digest copy. A digest listing whose newest copy is older than `recency_days` is cut as stale.
   - **Pass 1:** sends the survivors to Ollama in batches of 30. The model scores each one 0–100 from its title, employer, and location.
     - Batches are cut from a fixed, mixed order (a hash of title and employer), so one digest's or one board's listings are spread across batches instead of filling one.
   - **Repeatable scoring:**
     - Requests turn the model's thinking off (`model_thinking`).
     - The first request for each batch is greedy (temperature 0, fixed seed), so a batch that hasn't changed gets nearly the same scores on every run.
     - Only retries sample, each with its own seed.
   - **Score caps:**
     - After each pass, a score that matches a `score_caps` rule is lowered to that rule's cap. Rules can match on employer, title, or posting text.
     - The model's own score is kept as `capped_from`.
     - A rule can carry a flag, such as "clearance", which the shortlist shows.
   - **Already applied:** listings in `applied.txt` are cut before ranking and listed in their own shortlist section.
   - **Pass 2:** re-scores the top `rerank_top_n` listings that have a description, in small batches, with an excerpt from the posting's role section added.

`html_text.py` holds the HTML-to-text conversion that ingest and board fetch share. `location_filter.py` holds the search-area rule that board fetch and the screener share. `verdicts.py` reads your verdicts on shortlist rows and scores a run against them (see [Reviewing the shortlist](#reviewing-the-shortlist)).

## Requirements

- Python 3 (developed on 3.13).
- [Ollama](https://ollama.com) running locally, with a model that can return JSON. The default model is `gemma4:12b`.
- For ingest: an IMAP mailbox with the digest emails filtered into their own folder.
- For board fetch and slug discovery: internet access.

You can use either source on its own. The screener reads whatever is in `raw/` and `raw_boards/`.

## Setup

Run every command from the repository root; the scripts use relative paths.

1. **Create your config** and fill in the values (see [Configuration](#configuration)):
   ```bash
   cp pipeline_config.example.json pipeline_config.json
   ```
2. **Set the IMAP login and password** in the environment variables named by `email_user_env` and `email_password_env` (defaults `EMAIL_USER` and `EMAIL_PASSWORD`). The config holds only the variable names, never the credentials.
   ```bat
   rem Windows: saved for your user account, so new terminals and scheduled tasks see them
   setx EMAIL_USER "<your IMAP login>"
   setx EMAIL_PASSWORD "<your IMAP password>"
   ```
   ```bash
   # macOS / Linux: add to your shell profile
   export EMAIL_USER='<your IMAP login>'
   export EMAIL_PASSWORD='<your IMAP password>'
   ```
   `setx` only affects terminals opened afterwards, so open a new one before running ingest.
3. **List your boards**:
   ```bash
   cp boards.example.json boards.json
   ```
   Each entry is `{"platform": ..., "slug": ..., "company": ...}`, where `platform` is `greenhouse`, `lever`, `ashby`, or `smartrecruiters`. See [Finding board slugs](#finding-board-slugs) to look up slugs.
4. **Write the ranking prompt** at the path in `prompt_path` (default `screening_prompt.txt`). The screener won't start without it. See [Writing the ranking prompt](#writing-the-ranking-prompt).
5. **Pull the model**:
   ```bash
   ollama pull gemma4:12b
   ```
   To use a different model, set `SCREEN_MODEL` to its tag.

Your personal files are gitignored: `pipeline_config.json`, `boards.json`, `screening_prompt.txt`, `targets.txt`, `applied.txt`, any `.bat` wrappers, and the `raw/`, `raw_boards/`, `out/`, `logs/`, `feedback/`, and `processed/` folders. The `.example` files are the only config that gets committed, so keep them free of real values.

## Running

```bash
python job_ingest.py     # save new digest emails to raw/
python board_fetch.py    # refresh raw_boards/ from boards.json
python jobs_screen.py    # filter, rank, and write out/
python -m unittest       # run the tests
```

The tests use the standard library's `unittest` and live in `tests/`. They cover listing identity (deduplication, applied jobs, batch order), score caps, the per-employer limit, and the verdict files. They write their fixtures to a temporary folder and need no personal config.

Results:
- `out/shortlist_<date>.md` is the top 25 as a table of score, flags, title, employer, location, link, and the model's one-line reason.
  - A ▲ before the score marks a listing re-scored in pass 2.
  - Flags come from `score_caps` rules; `capped` means a rule lowered the model's score.
  - With `shortlist_per_employer` set, a line under the table counts the postings left out for that limit.
  - Jobs in `applied.txt` that are still posted are listed after the table.
  - `feedback/<date>.md` is written alongside, ready for your verdicts.
  - Board rows link straight to the posting. Digest rows have no link, so open the digest email to find them.
- `out/ranked_<date>.json` holds:
  - every ranked listing with its score and reason. Re-scored ones also carry `pass1_score` and `rescored`; ones matching a cap rule carry `score_cap`, `flags`, and, if lowered, `capped_from`; shortlisted ones carry `shortlist_rank`;
  - every cut listing with its `reject_reason` (`applied`, `excluded employer`, `excluded title pattern`, `outside area`, or `stale digest`);
  - `failed_batches` and `rerank_failed_batches`.

When something fails:
- **Ingest** exits non-zero if the config is incomplete, the server's TLS certificate can't be verified, the folder can't be opened, or a message can't be fetched. It gives up on a server that doesn't respond within 60 seconds, and it never falls back to the inbox.
- **The screener** retries any malformed model response, up to 3 attempts per batch.
  - Indices numbered from 1 are corrected, and numbering that can't be determined is rejected rather than guessed.
  - A batch still without usable scores, or whose connection fails, is left as 0/"unscored", and 3 failed batches in a row stop the pass.
  - **Nothing scored:** exits with code 2 and writes nothing, which usually means Ollama isn't running or the model isn't pulled.
  - **Some pass-1 batches failed:** still writes the shortlist with a warning at the top, then exits 1.
  - **Pass 2 fails or is skipped** (it's skipped when pass 1 had to stop): those listings keep their pass-1 scores, with a note in the shortlist.
- **Board fetch** reports a failed board and moves on to the next one. A malformed `boards.json` entry (no slug, unknown platform) is reported and skipped the same way. A failed board's files aren't archived. Once every board has been tried, it exits 1 if any board failed. A failed SmartRecruiters description fetch is retried on the next run.

## Reviewing the shortlist

A short daily loop keeps the ranking honest, and gives it something to be measured against:

1. **Give each row a verdict.** With each shortlist, the screener writes `feedback/<date>.md`, one line per row:
   ```
   12 [?] Senior Backend Engineer | Acme ::
   ```
   - Replace `?` with `y` (would apply), `m` (maybe), `n` (no), or `a` (applied), and add notes after `::`.
   - Keep `title | employer` as written: it identifies the listing, so a verdict stays valid on later days.
   - Other lines are free notes, ignored by the parser.
   - The file is never overwritten, so a rerun the same day keeps what you've filled in. The `feedback/` folder is gitignored.
2. **Record applications.** An `a` verdict cuts that listing from future rankings. For jobs you applied to outside the shortlist, add a line to `applied.txt`:
   - the posting's URL for a board row;
   - `Title | Employer` for a digest row, since those have no link.

   Lines starting with `#`, and anything after ` #`, are comments. A `Title | Employer` line is split at the last pipe, so a title that contains one is fine. Applied jobs are skipped when ranking and listed under "Already applied, still posted" while the posting stays up.
3. **Measure:** `python verdicts.py` scores the latest run (or `python verdicts.py out/ranked_<date>.json`) against every verdict so far. It reports:
   - how often the run's scores order a pair of your verdicts the way you did, with equal scores counting half: 50% is random, 100% is your order;
   - how many y/m/n listings made the shortlist;
   - the "yes" listings it missed and the "no" listings it let in.

   Verdicts that shaped a change score that change in-sample; the next days' verdicts show whether it generalizes.
4. **Turn repeated notes into rules:**
   - A pattern that's mechanical (a title word, a phrase in the posting, an employer) belongs in config: `excluded_title_patterns` to drop it, `score_caps` to limit it, `shortlist_per_employer` for crowding.
   - A judgment call belongs in the ranking prompt.
   - Check a change on the next shortlist, or with a scratch run: import `jobs_screen`, point `jobs_screen.OUT_DIR` and `jobs_screen.TEMPLATE_DIR` (where the verdict template goes) at a scratch folder, and call `jobs_screen.main()`. A scratch run still reads your `a` verdicts from `feedback/`, so applied jobs are cut as in a real run.

## Scheduled runs (Windows)

To have the shortlist waiting each morning, run the scripts from Windows Task Scheduler:

1. **Create two wrapper batch files** in the repo. `*.bat` is gitignored, so wrappers stay local.
   - The ingest wrapper runs `job_ingest.py`.
   - The screening wrapper runs `board_fetch.py`, then `jobs_screen.py`.
   - Each wrapper should:
     - start with `cd /d "%~dp0"` so it runs from its own folder;
     - `set PYTHONUTF8=1`, because Python on Windows otherwise writes redirected output in the ANSI code page and crashes on characters outside it;
     - append unbuffered output to a file in `logs\`, for example `python -u job_ingest.py >> logs\ingest.log 2>&1`.
   - **Retries:** retrying ingest on any failure is safe. Retry the screener only on exit code 2: nothing was written in that case, while exit 1 means a partial run that a retry would score again from scratch.
2. **Schedule the ingest task** for after your digests usually arrive (a second, later run catches stragglers cheaply). Schedule the screening task a few hours later.
3. **Change the task settings:**
   - Turn on "Wake the computer to run this task".
   - Turn on "Run task as soon as possible after a scheduled start is missed".
   - Set "Stop the task if it runs longer than" to a few hours, so a hung run doesn't block the next day's run.

   "Run only when user is logged on" is fine, and necessary if your mail server or Ollama runs in your login session.

The current schedule, wrapper behavior, and task settings are in the Automation section of [STATUS.md](STATUS.md).

## Configuration

`pipeline_config.json` holds every personal setting. Each script stops at startup if a key it requires is missing.

### Mail (`job_ingest.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `email_user_env` | **Name** of the environment variable that holds the IMAP login. Default `EMAIL_USER`. | `"EMAIL_USER"` |
| `email_password_env` | **Name** of the environment variable that holds the IMAP password. Default `EMAIL_PASSWORD`. | `"EMAIL_PASSWORD"` |
| `imap_host` | IMAP server hostname. Required. | `"imap.example.com"` |
| `imap_port` | IMAP port. Default `993`. | `993` |
| `imap_starttls` | `true` connects in plain text and upgrades with STARTTLS before logging in, which some local IMAP servers require. `false` (the default) uses TLS from the start. | `false` |
| `imap_tls_verify` | Verify the server's TLS certificate. Default `true`. `false` turns the check off, for a local mail bridge with a self-signed certificate; ingest accepts it only when `imap_host` is a loopback address (`localhost`, `127.0.0.1`, `::1`). | `true` |
| `imap_cafile` | Path to a PEM certificate or CA bundle to trust *instead of* the system store, such as a bridge's exported certificate. The certificate must also match `imap_host`. Optional. | `"bridge.pem"` |
| `mail_folder` | IMAP folder that holds only the digest emails. Required. Ingest reads every message in it. | `"Jobs/Alerts"` |

Both `_env` keys must be valid variable names (letters, digits, underscores). Ingest refuses any other value without printing it, because a value like that is probably the login or password itself.

### Search area (`board_fetch.py` and `jobs_screen.py`)

Board fetch applies these to postings, using the platforms' structured remote and country fields where they exist. The screener applies them to digest listings, using the location text alone, before ranking.

| Key | Purpose | Example |
|-----|---------|---------|
| `metro_cities` | A listing is kept if its location contains any of these names (case-insensitive substring, so `"springfield"` also matches "West Springfield"). Remote listings open to the US are kept regardless. Names are combined into a regular expression, so avoid characters like `(`, `+`, `.`. An empty list matches every location. | `["springfield", "riverton"]` |
| `remote_regions` | Extra place names, such as your state and its abbreviation, that make a remote listing count as US-based when there's no country field. Matched as whole words, ignoring case. Without it, only US-wide wording ("US", "USA", "United States", "North America") or a bare "Remote" counts. Optional. | `["state name", "ST"]` |

### Board fetch (`board_fetch.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `pay_floor` | Annual base salary in USD. A posting is cut only when the **top** of its published range is below this; postings without published pay are kept. Pay is read from Greenhouse pay-transparency sections and Ashby salary data (hourly rates × 2080). Lever and SmartRecruiters postings never have pay parsed. Missing or `0` disables the cut. | `100000` |
| `included_title_patterns` | Python regular expressions. A board posting is fetched only if its title matches at least one, ignoring case. Matching is by substring, so `"analyst"` also matches "Analysts". An empty list fetches every title. Applies to board postings only. | `["analyst", "data ?engineer"]` |
| `excluded_keywords` | Plain phrases, checked after `included_title_patterns`. A board posting is skipped if its title contains one as a whole word, ignoring case: `"sales engineer"` blocks "Sales Engineer II" but not "Sales Platform Engineer". Applies to board postings only. | `["intern", "recruiter", "sales engineer"]` |

### Hard filters (`jobs_screen.py`, both sources)

| Key | Purpose | Example |
|-----|---------|---------|
| `excluded_employers` | Plain company names, matched as whole words, ignoring case: `"Acme"` cuts "Acme Corp" but not "Acmeworks". An empty list excludes nothing. | `["Acme", "Initech"]` |
| `excluded_title_patterns` | Python regular expressions, searched in the title, ignoring case. JSON needs doubled backslashes: write `"\\bintern\\b"` for `\bintern\b`. | `["\\bintern(ship)?\\b", "summer 20\\d\\d"]` |

### Digest parsing (`jobs_screen.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `digest_noise_lines` | Header or menu lines in your digest emails that can fall within the three lines above a `View` line and be misread as a listing. A candidate listing is dropped if its title, employer, or location equals one of these, ignoring case. Common table headers ("Job Title", "Employer", "Location", "Action") are handled already. Optional. | `["saved search name", "manage alerts"]` |

### Recency and ranking

| Key | Purpose | Default |
|-----|---------|---------|
| `recency_days` | Digests older than this many days (by their Date header) are skipped by ingest and cut as stale by the screener. A listing still present in a newer digest stays. Board postings are exempt, because closed postings are archived instead. Digests with an unreadable date are kept, with a warning. `0` or missing disables the window. | `0` |
| `rerank_top_n` | How many of the top pass-1 listings that have a description get re-scored in pass 2. `0` turns pass 2 off. | `15` |
| `rerank_batch` | Listings per pass-2 request. | `5` |
| `excerpt_chars` | Maximum length of the posting excerpt sent per listing in pass 2. | `1500` |
| `model_thinking` | Let a thinking model reason before it answers. `false` (default): about 18 s per pass-1 batch, and repeatable scores, because the first request is greedy; the model follows the prompt's caps less reliably, which `score_caps` covers. `true`: about 4× slower, scores vary from run to run, and long reasoning can overflow the context into empty or truncated responses, which are retried. Greedy decoding isn't used with thinking on, because the reasoning doesn't terminate. | `false` |
| `score_caps` | Upper score limits applied in code after each pass. Optional. See the note below. | `[{"employers": ["Acme Staffing"], "max_score": 50}]` |
| `shortlist_per_employer` | At most this many shortlist rows from one employer; the rest are counted under the table and stay in the JSON. `0` or missing means no limit. | `2` |

The pass-2 excerpt starts at the posting's role section: a heading such as "What you'll do", "Responsibilities" or "Job description", or failing that a similar phrase in the first half of the description. Mentions inside closing EEO or pay text are ignored. It's cut at a sentence or line end.

`score_caps` is a list of rules. Each rule has a `max_score` and at least one of these:
- `employers`: plain names, matched like `excluded_employers` (whole words, ignoring case);
- `title_patterns`: regular expressions searched in the title;
- `description_patterns`: regular expressions searched in the stored posting text, so they apply to board postings only.

A rule matches a listing when any of its lists matches, and a listing that matches several rules gets the lowest cap. A rule without a `max_score`, or with none of the three lists, stops the screener at startup, as does an invalid regular expression here or in `excluded_title_patterns`. An optional `flag` names the rule in the shortlist's Flags column whenever it matches, even if the score was already under the cap. Patterns ignore case and, like other regexes in JSON, need doubled backslashes.

```json
"score_caps": [
  {"employers": ["Not Available"], "max_score": 40},
  {"title_patterns": ["^(?!.*\\bsenior\\s*(/|or)\\s*(staff|principal)\\b).*\\b(staff|principal)\\b"], "max_score": 35},
  {"description_patterns": ["\\bts\\s*/\\s*sci\\b", "\\bsecurity clearance\\b"], "max_score": 40, "flag": "clearance"}
]
```

The title rule caps staff and principal titles but exempts multi-level postings such as "Senior / Staff Engineer". A description pattern can't tell "clearance required" from "no clearance required", so check what a new pattern catches in `ranked_<date>.json` (the `flags` field).

### Files

| Key | Purpose | Default |
|-----|---------|---------|
| `prompt_path` | System prompt for the ranking model (`jobs_screen.py`). The file must exist. | `"screening_prompt.txt"` |
| `applied_path` | Jobs you've applied to (see [Reviewing the shortlist](#reviewing-the-shortlist)). Optional; a missing file means none. | `"applied.txt"` |

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `EMAIL_USER` (or the name in `email_user_env`) | IMAP login. Required for ingest. | none |
| `EMAIL_PASSWORD` (or the name in `email_password_env`) | IMAP password. Required for ingest. | none |
| `OLLAMA_URL` | Ollama server address. | `http://localhost:11434` |
| `SCREEN_MODEL` | Ollama model tag used for ranking. | `gemma4:12b` |
| `EMAIL_IMAP_HOST`, `EMAIL_IMAP_PORT` | Fallbacks, used only when `imap_host` / `imap_port` are missing from the config. | none |
| `PYTHONUTF8` | Set to `1` on Windows so output redirected to log files is written as UTF-8. Set it in your scheduled-run wrappers, or once with `setx`. | not set |

## Writing the ranking prompt

The prompt file is sent as the system message with every batch. The model sees one line per listing:

```
0: Project Coordinator | Acme | Springfield (Remote) ($90,000-$110,000)
```

Board postings add `(Remote)` when the platform flags the role as remote, and the pay range in parentheses when one was found. Digest rows carry only title, employer, and location.

A good prompt covers:
- the roles and seniority you're targeting, and the ones you aren't
- your core skills, and domains to score down
- location rules (your metro area, remote)
- how to treat staffing agencies and unknown employers
- a 0–100 scoring rubric with bands, so scores spread out instead of clustering. Make the bands **absolute**: say what a listing needs to reach each band (for example, "75+ requires a title at your level, your core stack, a direct employer, and nothing that lowers your odds"). Don't use per-batch quotas such as "at most 2 scores above 75". A quota makes a listing's score depend on whichever listings share its request.

In pass 2, each listing line is followed by `POSTING: <excerpt>`, listings are separated by `---`, and the script appends a short second-pass section to your prompt. That section tells the model to use the excerpt, to revise scores up or down on that evidence, and to treat the excerpt as evidence rather than instructions. So write your prompt to apply to both passes: describe what makes a good match, not only what a title implies.

The script adds the JSON response format and batch instructions itself, including that each listing is scored on its own, so don't define an output format in the prompt. The file is plain text with no templating.

A hard limit the prompt states ("staffing agencies score at most 50") is best repeated in `score_caps`. The prompt keeps the model's reasoning consistent, and the config enforces the limit. Repeating it matters most for limits that depend on posting text, such as a required security clearance: that text often sits past the pass-2 excerpt, so the model never sees it.

## Finding board slugs

Put company names in `targets.txt`, one per line, then run:

```bash
python slug_probe.py targets.txt
```

The script tries common slug spellings on all four platforms and prints a `boards.json` line for each slug that returns jobs. **VERIFIED only means the slug has postings**: a generic name can match a different company's board, so open the board and check it before adding the line. With no file argument, it takes employers that scored 60 or higher from the latest `out/ranked_*.json`.

## Tuning tips

- After changing a board filter, re-run `board_fetch.py`. Postings that no longer pass move to `raw_boards/archive/` and drop out of ranking.
- When you change how board files are written (layout or description text), bump `FILE_FORMAT` in `board_fetch.py`. Every live posting is then rewritten on the next run, with no manual cleanup.
- If batches fail with JSON parse errors, lower `BATCH = 30` in `jobs_screen.py`.
- To re-ingest after changing how emails are converted to text, delete `raw/job_uid_*.txt` and run ingest again. Existing files are skipped.
- Known issues and planned work are tracked in [STATUS.md](STATUS.md).
