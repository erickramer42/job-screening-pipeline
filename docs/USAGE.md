# Usage

How to set up and run the pipeline day to day. The [README](../README.md) gives the overview. [CONFIGURATION.md](CONFIGURATION.md) lists every setting, and [STATUS.md](../STATUS.md) covers current state and planned work.

## How a run works

```
job_ingest.py    mail folder (IMAP)     ->  raw/job_uv<UIDVALIDITY>_uid<UID>.txt
board_fetch.py   boards.json (APIs)     ->  raw_boards/<platform>_<id>.txt (with description)
jobs_screen.py   raw/ + raw_boards/     ->  recency + hard filters -> pass 1 -> pass 2 -> out/
```

1. **Ingest** saves each digest email as a text file.
   - With `recency_days` set, the folder is searched with SINCE, so the server leaves older digests out. SINCE counts the arrival date in whole days, so one extra day is searched; the screener cuts a stale digest by its Date header anyway.
   - Mail is opened read-only, so emails are never marked read, moved, or deleted.
   - Each job in the email must appear as four lines: title, employer, location, and a `View` link.
   - A file is named for the folder's UIDVALIDITY and the message's UID. If the server ever resets its UIDs, the window is ingested again, rather than new digests being skipped as already saved.
2. **Board fetch** pulls each configured board. It keeps postings whose titles match your patterns and whose location is in your metro area or remote within the US.
   - It drops postings whose published pay range tops out below your floor.
   - Some Greenhouse boards give only the work arrangement as the location ("Hybrid", "In-Office", "Distributed"). For those, the posting's office locations are added, as in `Hybrid: Springfield, ST`, and the filter reads them.
     - Offices in your area come first, then the rest by name. At most three are named, and the others are counted (`; +4 more`). So the location line stays short and doesn't change if the board lists its offices in another order. A posting kept with the full list is still kept.
     - "Distributed" counts as remote. With no country field, a remote posting is kept only when its text names a metro city or the US, so a "Distributed" posting is kept when one of its offices does, and dropped when it has none.
     - An office labeled remote ("Remote Spain") is left out of a Hybrid or In-Office location, since that isn't where the job is worked. On a Distributed posting, it's kept.
   - Postings that close, or stop passing the filters, move to `raw_boards/archive/`. So do the files of a board you remove from `boards.json`.
   - Each posting's description is stored below the listing, from every supported platform, along with the date the board first published it.
   - A posting's file is rewritten whenever its text would change: the employer edited the title, location, pay or description, or an older version of the script wrote the file.
   - SmartRecruiters descriptions cost a request per posting, so they're fetched again only when the listing lines change. A new published date alone rewrites the file but keeps its description.
   - Files are written through a temporary file, so an interrupted run never leaves a truncated one.
3. **Screen** reads both folders and cuts excluded employers and title patterns. It also cuts digest listings outside your area, using the same metro-or-US-remote rule that board fetch applies to postings.
   - **Duplicates and staleness:** a listing that appears in several files is kept once.
     - The board copy, with its link, pay and description, is preferred. Otherwise the newest digest copy is kept.
     - A digest listing whose newest copy is older than `recency_days` is cut as stale.
   - **Pass 1:** sends the survivors to Ollama in batches of `pass1_batch` (30). The model scores each one 0–100 from its title, employer, and location.
     - Batches are cut from a fixed, mixed order (a hash of title and employer). One digest's or one board's listings are spread across batches instead of filling one.
   - **Repeatable scoring:**
     - Requests turn the model's thinking off (`model_thinking`).
     - The first request for each batch is greedy (temperature 0, fixed seed), so a batch that hasn't changed gets nearly the same scores on every run.
     - Only retries sample, each with its own seed.
   - **Score caps:**
     - After each pass, a score that matches a `score_caps` rule is lowered to that rule's cap. Rules can match on employer, title, or posting text.
     - The model's own score is kept as `capped_from`.
     - A rule can carry a flag, such as "clearance", which the shortlist shows.
   - **Already applied:** listings in `applied.txt`, or marked `a` in a feedback file, are cut before ranking and listed in their own shortlist section. A board row marked `a` is matched by its posting URL too, so it stays cut if the employer edits the title.
   - **Pass 2:** re-scores the top `rerank_top_n` listings that have a description, in small batches. Each one comes with an excerpt from the posting's role section.
   - **"No" verdicts:** after both passes, a listing whose latest verdict is `n` is held at `no_verdict_cap` (50) and flagged `judged no`. It stays in the ranking, below the listings you haven't ruled out, including those the model scored at the cap itself. A board row is matched by its posting URL too. The cap comes after pass 2, so it doesn't change which listings pass 2 re-scores, or any model score.
   - **Two shortlist sections:** the listings pass 2 scored against their posting text fill the main section (`shortlist_size`, 15). The rest have only a title-only score and go in a "Needs lookup" section (`lookup_size`, 10). That's every digest row, plus any board posting pass 2 didn't re-score: one below the top `rerank_top_n`, one without a description, or one whose batch failed. Each section is ranked on its own, since a title-only score rests on far less evidence.

The scripts share three small modules:
- `html_text.py`: the HTML-to-text conversion, used by ingest and board fetch.
- `location_filter.py`: the search-area rule, used by board fetch and the screener.
- `pipeline_common.py`: config loading, the description marker in board files, email date parsing, and atomic writes, used by all three scripts.

`verdicts.py` reads your verdicts on shortlist rows and scores a run against them (see [Reviewing the shortlist](#reviewing-the-shortlist)).

## Requirements

- Python 3 (developed on 3.13). Everything is standard library; there is nothing to `pip install`.
- [Ollama](https://ollama.com) running locally, with a model that can return JSON. The default model is `gemma4:12b`.
- For ingest: an IMAP mailbox with the digest emails filtered into their own folder.
- For board fetch and slug discovery: internet access.

You can use either source on its own. The screener reads whatever is in `raw/` and `raw_boards/`.

## Setup

Run every command from the repository root; the scripts use relative paths.

1. **Create your config** and fill in the values (see [CONFIGURATION.md](CONFIGURATION.md)):
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

Your personal files are gitignored:
- `pipeline_config.json`, `boards.json`, `screening_prompt.txt`, `targets.txt` and `applied.txt`;
- any `.bat` wrappers;
- the `raw/`, `raw_boards/`, `out/`, `logs/`, `feedback/`, and `processed/` folders.

The `.example` files are the only config that gets committed, so keep them free of real values.

## Running

```bash
python job_ingest.py     # save new digest emails to raw/
python board_fetch.py    # refresh raw_boards/ from boards.json
python jobs_screen.py    # filter, rank, and write out/
python -m unittest       # run the tests
```

The tests use the standard library's `unittest` and live in `tests/`. They cover:
- listing identity (deduplication, applied jobs, batch order), score caps, the per-employer limit, and the verdict files;
- the model requests and responses, and the pass-2 excerpt;
- the screener's exit codes, with `main()` run end to end and a stand-in for the model;
- the board-file layout and when a file is rewritten or archived;
- email decoding, digest file names, and an ingest run against a stand-in mailbox, which may only examine and peek;
- the scheduled wrappers' date decisions (`wrapper_helper.py`): when screening starts, and the failure marker;
- the contract between them: a file ingest or board fetch writes must parse into listings in the screener.

They write their fixtures to a temporary folder, mock the network, and need no personal config.

## Outputs

- `out/shortlist_<date>.md` has two tables, each with score, flags, title, employer, location, posted date, link, and the model's one-line reason:
  - **Read against the posting:** the top `shortlist_size` (15) listings pass 2 re-scored. It can come up short: when the re-scored listings come from only a few employers, `shortlist_per_employer` caps how many it can show, and when pass 2 fails or is skipped, it's empty.
  - **Needs lookup:** the top `lookup_size` (10) of the rest, scored on title, employer and location alone. Open the posting before judging one.
  - Rows are numbered on through both tables, as in the verdict template.
  - A ▲ before the score marks a listing re-scored in pass 2.
  - Posted is the date the board first published the posting. Digest rows show `-`. Board postings are exempt from `recency_days`, so this is how to spot one that has been open for months.
    - Lever's API has no publish date, so a Lever row shows the date the posting was created, which can be before it went live.
  - Flags come from `score_caps` rules, plus `judged no` for a listing your latest verdict ruled out. `capped` means a rule or that verdict lowered the model's score.
  - With `shortlist_per_employer` set, it applies across both tables, and a line under the tables counts the postings left out for that limit.
  - Jobs in `applied.txt` that are still posted are listed after the tables.
  - `feedback/<date>.md` is written alongside, ready for your verdicts. Rows flagged `judged no` are left out of it, so you aren't asked again.
  - Board rows link straight to the posting. Digest rows have no link, so open the digest email to find them.
- `out/ranked_<date>.json` holds:
  - **What produced the run** (`run`): when it started, the model and whether thinking was on, 12-character fingerprints of the ranking prompt (`prompt_sha`), the pass-2 addendum and the `score_caps` rules, and the settings that affect scores. Two runs with the same `prompt_sha` used the same prompt.
  - **Every ranked listing**, with its score and reason, plus these fields where they apply:
    - re-scored listings: `pass1_score`, `rescored`, and the `excerpt` pass 2 sent, plus `pass1_capped_from` when a cap lowered the pass-1 score;
    - listings that match a cap rule: `score_cap` and `flags`, plus `capped_from` if the cap lowered the score;
    - listings judged `n`: the `judged no` flag, plus `verdict_capped_from` if `no_verdict_cap` lowered the score. Compare runs' scores on that field when it's there, since it's the score before the verdict;
    - shortlisted listings: `shortlist_rank`; listings `shortlist_per_employer` kept off the shortlist: `employer_limited`;
    - board listings: `posted`, when the board gives a date.
  - **Every cut listing**, with its `reject_reason`: `applied`, `excluded employer`, `excluded title pattern`, `outside area`, or `stale digest`.
  - `failed_batches` and `rerank_failed_batches`.

  The posting text isn't copied into the JSON: it's in each row's `source_file`.
- `out/FAILED_<date>.md` appears when a scheduled screening run fails (see [Scheduled runs](#scheduled-runs-windows)).

## When something fails

- **Config mistakes** stop every script at startup with exit 1, and one line naming `pipeline_config.json`: a missing required key, a bad value or JSON syntax, or an invalid regular expression.
- **Ingest** exits non-zero if:
  - the config is incomplete;
  - the server's TLS certificate can't be verified;
  - the folder can't be opened, or the server doesn't report its UIDVALIDITY;
  - or a message can't be fetched.

  It gives up on a server that doesn't respond within 60 seconds, and it never falls back to the inbox. Email bodies are decoded with the charset they declare. A message with no text part is saved without listings, so it isn't downloaded again; the summary line counts it.
- **Before scoring**, the screener checks Ollama.
  - If Ollama can't be reached, it exits with code 2 and writes nothing.
  - If it's running without the model, it exits 1 straight away, since a retry wouldn't help.
- **The screener** retries any malformed model response, up to 3 attempts per batch.
  - Indices numbered from 1 are corrected. Numbering that can't be determined is rejected rather than guessed.
  - If a retry's connection fails, the scores from the attempt before it are kept.
  - A batch still without usable scores, or whose connection fails, is left as 0/"unscored". 3 failed batches in a row stop the pass.
  - **Ollama rejects the request** (an HTTP 4xx, for example `model_thinking` on for a model that can't think): the pass stops at once and Ollama's reason goes to the log. If nothing was scored, it exits 1 and writes nothing, since a retry would send the same request.
  - **Nothing scored** otherwise: exits with code 2 and writes nothing, which usually means Ollama stopped during the run. A server error from Ollama (5xx) is logged with its reason and counts the same way.
  - Every output file is written through a temporary file, so a run killed mid-write leaves the previous file or the complete new one. Ingest and board fetch write the same way.
  - **Some pass-1 batches failed:** still writes the shortlist with a warning at the top, then exits 1.
  - **Pass 2 fails or is skipped** (it's skipped when pass 1 had to stop): those listings keep their pass-1 scores, with a note in the shortlist, and the run exits 1.
  - **A recent digest yields no listings** though it has text: the shortlist names the file in a warning, since the digest's format has probably changed. The exit code doesn't change.
- **Board fetch** reports a failed board and moves on to the next one.
  - A malformed `boards.json` entry (no slug, unknown platform) is reported and skipped the same way.
  - A failed board's files aren't archived.
  - Each board's log line starts with how many postings the API returned, so an empty board (a stale slug, say) can be told from one whose postings were all filtered out.
  - The files of a board no longer in `boards.json` are archived, except in a run where an entry is malformed, since that entry's files would look unconfigured.
  - Once every board has been tried, it exits 1 if any board failed.
  - A failed SmartRecruiters description fetch is retried on the next run.

## Reviewing the shortlist

A short daily loop keeps the ranking honest, and gives it something to be measured against:

1. **Give each row a verdict.** With each shortlist, the screener writes `feedback/<date>.md`, one line per row:
   ```
   12 [?] Senior Backend Engineer | Acme ::
   ```
   - Replace `?` with `y` (would apply), `m` (maybe) or `n` (no), and add notes after `::`.
   - Add `a` when you've applied, with a comma: `[m,a]` is a maybe you applied to anyway, and `[a]` alone records only the application.
   - A row whose flags can't be read is reported with its file and line number whenever the screener or `verdicts.py` reads the folder. That covers an unknown letter, or two verdicts like `[y,n]`. It isn't skipped silently.
   - Keep `title | employer` as written: it identifies the listing, so a verdict stays valid on later days. A board row is also matched by its posting URL, from that day's `out/ranked_<date>.json`, so its verdict survives an edit to the title.
   - A later line on the same listing updates your verdict rather than replacing it. `[a]` alone keeps the earlier y/m/n, an empty note keeps the earlier note, and "applied" stays set once given.
   - Other lines are free notes, ignored by the parser.
   - **A rerun the same day:** while the file is still the untouched template, it's rewritten for the rerun's rows. Once you've filled anything in, only the rows new to the list are appended, under a `# Rerun at HH:MM` comment; nothing already in the file changes. The `feedback/` folder is gitignored.
   - **An `n` holds the listing down** at `no_verdict_cap` (50) on later shortlists, flagged `judged no`, until a later verdict changes it. It isn't cut, so you can still come back to it, but later templates leave it out. To change the verdict, add a line for it by hand.
2. **Record applications.** An `a` flag cuts that listing from future rankings, and `verdicts.py` then leaves it out of its figures, since the ranker no longer places it. For jobs you applied to outside the shortlist, add a line to `applied.txt`:
   - the posting's URL for a board row;
   - `Title | Employer` for a digest row, since those have no link.

   Lines starting with `#`, and anything after ` #`, are comments. A `Title | Employer` line is split at the last pipe, so a title that contains one is fine. A line that is neither a URL nor `Title | Employer` is reported with its line number and ignored. Applied jobs are skipped when ranking. While the posting stays up, they're listed under "Already applied, still posted".
3. **Measure:** `python verdicts.py` scores the latest run against every verdict so far; pass a file (`python verdicts.py out/ranked_<date>.json`) to score another run. It reports:
   - how often the run's scores order a pair of your verdicts the way you did, with equal scores counting half: 50% is random, 100% is your order. A listing held down by your `n` counts with its score from before that cap, since the cap is your verdict, not the ranker's judgment;
   - how many y/m/n listings made the shortlist;
   - the "yes" listings it missed and the "no" listings it let in. A listed row shows its place on the shortlist, and a missed row says when the per-employer limit held it out. A listing held down by your `n` shows the model's score, then the score it was held at.

   Verdicts that shaped a change score that change in-sample; the next days' verdicts show whether it generalizes.
4. **Turn repeated notes into rules:**
   - A pattern that's mechanical belongs in config: a title word, a phrase in the posting, an employer. Use `excluded_title_patterns` to drop it, `score_caps` to limit it, and `shortlist_per_employer` for crowding.
   - A judgment call belongs in the ranking prompt.
   - Check a change on the next shortlist, or with a scratch run:
     - Import `jobs_screen`.
     - Point `jobs_screen.OUT_DIR` and `jobs_screen.TEMPLATE_DIR` (where the verdict template goes) at a scratch folder.
     - Call `jobs_screen.main()`.

     A scratch run still reads your `a` verdicts from `feedback/`, so applied jobs are cut as in a real run.

## Scheduled runs (Windows)

To have the shortlist waiting each morning, run the scripts from Windows Task Scheduler:

1. **Copy the wrappers:** `run_ingest.bat.example` to `run_ingest.bat`, and `run_screen.bat.example` to `run_screen.bat`. `*.bat` is gitignored, so your copies stay local; the examples hold no personal data. They:
   - start with `cd /d "%~dp0"` so they run from their own folder;
   - `set PYTHONUTF8=1`, because Python on Windows otherwise writes redirected output in the ANSI code page and crashes on characters outside it;
   - append unbuffered output to `logs\ingest.log`, `logs\boards.log` and `logs\screen.log`, trim them to 2,000 lines, and write one line per failed step to `logs\errors.log`.
   - **Retries:** ingest is retried on any failure, 3 attempts 10 minutes apart. The screener is retried only on exit code 2, when nothing was written, 3 attempts 15 minutes apart, and the Ollama app is started before each retry unless it's running. Exit 1 means a partial run that a retry would score again from scratch, or a problem a retry can't fix.
   - **Once a day:** the screening wrapper asks `wrapper_helper.py begin 06:00` whether to run. It skips a start before 06:00, and one after today's shortlist was written. Change `06:00` if your daily screening time differs.
   - **A failed morning:** after the screener's last failed attempt, `out/FAILED_<date>.md` says what the exit code means and quotes the attempt's error lines. A later run that day removes it.
2. **Schedule the ingest task** for after your digests usually arrive (a second, later run catches stragglers cheaply). Schedule the screening task a few hours later, at the time the screening wrapper names.
3. **Add an "At log on" trigger to both tasks**, with a delay (5 minutes for ingest, 15 for screening). After an update restart that leaves no one signed in, the night's runs then happen once you sign in.
4. **Change the task settings:**
   - Turn on "Wake the computer to run this task".
   - Turn on "Run task as soon as possible after a scheduled start is missed".
   - Set "Stop the task if it runs longer than" to a few hours, so a hung run doesn't block the next day's run.

   "Run only when user is logged on" is fine. It's necessary if your mail server or Ollama runs in your login session.

The current schedule, wrapper behavior, and task settings are in the [Automation section of STATUS.md](../STATUS.md#automation).

## Writing the ranking prompt

The prompt file is sent as the system message with every batch. The model sees one line per listing:

```
0: Project Coordinator | Acme | Springfield (Remote) ($90,000-$110,000)
```

Board postings add `(Remote)` when the platform flags the role as remote, and the pay range in parentheses when one was found. Digest rows carry only title, employer, and location.

A good prompt covers:
- the roles and seniority you're targeting, and the ones you aren't;
- your core skills, and domains to score down;
- location rules (your metro area, remote);
- how to treat staffing agencies and unknown employers;
- a 0–100 scoring rubric with bands, so scores spread out instead of clustering.
  - Make the bands **absolute**: say what a listing needs to reach each band. For example: "75+ requires a title at your level, your core stack, a direct employer, and nothing that lowers your odds".
  - Don't use per-batch quotas such as "at most 2 scores above 75". A quota makes a listing's score depend on whichever listings share its request.

In pass 2:
- each listing line is followed by `POSTING: <excerpt>`, and listings are separated by `---`;
- the script appends a short second-pass section to your prompt. It tells the model to use the excerpt, to revise scores up or down on that evidence, and to treat the excerpt as evidence rather than instructions.

So write your prompt to apply to both passes: describe what makes a good match, not only what a title implies.

The script adds the JSON response format and batch instructions itself, including that each listing is scored on its own, so don't define an output format in the prompt. The file is plain text with no templating.

A hard limit the prompt states ("staffing agencies score at most 50") is best repeated in `score_caps`. The prompt keeps the model's reasoning consistent, and the config enforces the limit. Repeating it matters most for limits that depend on posting text, such as a required security clearance: that text often sits past the pass-2 excerpt, so the model never sees it.

## Finding board slugs

Put company names in `targets.txt`, one per line, then run:

```bash
python slug_probe.py targets.txt
```

The script tries common slug spellings on all four platforms and prints a `boards.json` line for each slug that returns jobs.
- **VERIFIED only means the slug has postings.** A generic name can match a different company's board.
  - Greenhouse and SmartRecruiters name the company on their postings, so the line says what the board calls itself, and starts that note with CHECK when it differs from the name you gave.
  - Ashby and Lever don't name it, so open the board and check it before adding the line.
- A company whose lookup fails (network down, a platform error) is reported as failed rather than as having no board, and the script then exits 1.
- With no file argument, it takes employers that scored 60 or higher from the latest `out/ranked_*.json`. A file argument that doesn't exist stops the script instead.

## Tuning tips

- After changing a board filter, re-run `board_fetch.py`. Postings that no longer pass move to `raw_boards/archive/` and drop out of ranking.
- When you change how board files are written (layout or description text), bump `FILE_FORMAT` in `board_fetch.py`. Every live posting is then rewritten on the next run, with no manual cleanup.
- If pass-1 responses come back truncated, lower `pass1_batch` in `pipeline_config.json`.
- To re-ingest after changing how emails are converted to text, delete `raw/job_*.txt` and run ingest again. Existing files are skipped.
- Known issues and planned work are tracked in [STATUS.md](../STATUS.md).
