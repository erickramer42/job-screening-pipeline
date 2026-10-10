# Job Screening Pipeline

Collects job listings from two sources, removes obvious mismatches with deterministic filters, ranks the rest with a local LLM, and writes a daily shortlist. It finds and ranks; you review the shortlist and apply yourself.

Sources:
- **Job-alert digest emails**: saved-search emails from a job site, read over IMAP from a dedicated mail folder. Each job in the email must appear as four lines: title, employer, location, and a `View` link.
- **Company career boards**: public posting APIs for Greenhouse, Lever, Ashby, and SmartRecruiters, for the companies you list.

Everything is plain Python standard library. There is nothing to `pip install`.

## How it works

```
job_ingest.py    mail folder (IMAP)     ->  raw/job_uid_<UID>.txt
board_fetch.py   boards.json (APIs)     ->  raw_boards/<platform>_<id>.txt
jobs_screen.py   raw/ + raw_boards/     ->  hard filters -> LLM ranking -> out/
```

1. **Ingest** saves each digest email as a text file. Mail is opened read-only, so emails are never marked read, moved, or deleted.
2. **Board fetch** pulls each configured board and keeps postings whose titles match your patterns and whose location is in your metro area or remote within the US. It drops postings whose published pay range tops out below your floor. Postings that close or stop passing the filters move to `raw_boards/archive/`.
3. **Screen** reads both folders and cuts excluded employers and title patterns. It then sends the survivors to Ollama in batches of 30. The model scores each one 0–100 from its title, employer, and location.

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

Your personal files are gitignored: `pipeline_config.json`, `boards.json`, `screening_prompt.txt`, `targets.txt`, any `.bat` wrappers, and the `raw/`, `raw_boards/`, `out/`, `logs/`, and `processed/` folders. The `.example` files are the only config that gets committed, so keep them free of real values.

## Running

```bash
python job_ingest.py     # save new digest emails to raw/
python board_fetch.py    # refresh raw_boards/ from boards.json
python jobs_screen.py    # filter, rank, and write out/
```

Results:
- `out/shortlist_<date>.md` is the top 25 as a table of score, title, employer, location, link, and the model's one-line reason. Board rows link straight to the posting. Digest rows have no link, so open the digest email to find them.
- `out/ranked_<date>.json` holds every ranked listing with its score and reason, every filtered-out listing with its `reject_reason`, and `failed_batches`.

When something fails:
- **Ingest** exits non-zero if the config is incomplete, the folder can't be opened, or a message can't be fetched. It gives up on a server that doesn't respond within 60 seconds, and it never falls back to the inbox.
- **The screener** exits with code 2 and writes nothing if no batch could be scored, which usually means Ollama isn't running or the model isn't pulled. If only some batches fail, it still writes the shortlist with a warning at the top, then exits 1.
- **Board fetch** reports a failed board and moves on to the next one. It still exits 0 when a fetch fails.

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
| `mail_folder` | IMAP folder that holds only the digest emails. Required. Ingest reads every message in it. | `"Jobs/Alerts"` |

Both `_env` keys must be valid variable names (letters, digits, underscores). Ingest refuses any other value without printing it, because a value like that is probably the login or password itself.

### Board fetch (`board_fetch.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `metro_cities` | A board posting is kept if its location contains any of these names (case-insensitive substring, so `"springfield"` also matches "West Springfield"). Remote postings open to the US are kept regardless. Names are combined into a regular expression, so avoid characters like `(`, `+`, `.`. An empty list matches every location. Digest rows aren't filtered by location in code; your ranking prompt handles that. | `["springfield", "riverton"]` |
| `remote_regions` | Extra place names, such as your state and its abbreviation, that make a remote posting count as US-based when the platform gives no country. Matched as whole words, ignoring case. Without it, only US-wide wording ("US", "USA", "United States", "North America") or a bare "Remote" counts. Optional. | `["state name", "ST"]` |
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

### Files

| Key | Purpose | Default |
|-----|---------|---------|
| `prompt_path` | System prompt for the ranking model (`jobs_screen.py`). The file must exist. | `"screening_prompt.txt"` |

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
- a 0–100 scoring rubric with bands, so scores spread out instead of clustering

The script adds the JSON response format and batch instructions itself, so don't define an output format in the prompt. The file is plain text with no templating.

## Finding board slugs

Put company names in `targets.txt`, one per line, then run:

```bash
python slug_probe.py targets.txt
```

The script tries common slug spellings on all four platforms and prints a `boards.json` line for each slug that returns jobs. **VERIFIED only means the slug has postings**: a generic name can match a different company's board, so open the board and check it before adding the line. With no file argument, it takes employers that scored 60 or higher from the latest `out/ranked_*.json`.

## Tuning tips

- After changing a board filter, re-run `board_fetch.py`. Postings that no longer pass move to `raw_boards/archive/` and drop out of ranking.
- If batches fail with JSON parse errors, lower `BATCH = 30` in `jobs_screen.py`.
- To re-ingest after changing how emails are converted to text, delete `raw/job_uid_*.txt` and run ingest again. Existing files are skipped.
- Known issues and planned work are tracked in [STATUS.md](STATUS.md).
