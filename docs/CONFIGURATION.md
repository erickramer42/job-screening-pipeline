# Configuration

`pipeline_config.json` holds every personal setting. Start from `pipeline_config.example.json`, which has placeholders only. Each script stops at startup, with one line naming the file, if a key it requires is missing, a value or the JSON itself is invalid, or a regular expression doesn't compile. Setup and day-to-day use are in [USAGE.md](USAGE.md).

## Mail (`job_ingest.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `email_user_env` | **Name** of the environment variable that holds the IMAP login. Default `EMAIL_USER`. | `"EMAIL_USER"` |
| `email_password_env` | **Name** of the environment variable that holds the IMAP password. Default `EMAIL_PASSWORD`. | `"EMAIL_PASSWORD"` |
| `imap_host` | IMAP server hostname. Required. | `"imap.example.com"` |
| `imap_port` | IMAP port. Default `993`. | `993` |
| `imap_starttls` | `true` connects in plain text and upgrades with STARTTLS before logging in, which some local IMAP servers require. `false` (the default) uses TLS from the start. | `false` |
| `imap_tls_verify` | Verify the server's TLS certificate. Default `true`. `false` turns the check off, for a local mail bridge with a self-signed certificate. Ingest accepts `false` only when `imap_host` is a loopback address (`localhost`, `127.0.0.1`, `::1`). | `true` |
| `imap_cafile` | Path to a PEM certificate or CA bundle to trust *instead of* the system store, such as a bridge's exported certificate. The certificate must also match `imap_host`. Optional. | `"bridge.pem"` |
| `mail_folder` | IMAP folder that holds only the digest emails. Required. Ingest reads every message in it. | `"Jobs/Alerts"` |

Both `_env` keys must be valid variable names (letters, digits, underscores). Ingest refuses any other value without printing it, because a value like that is probably the login or password itself.

## Search area (`board_fetch.py` and `jobs_screen.py`)

Board fetch applies these to postings, using the platforms' structured remote and country fields where they exist. A Greenhouse location that names only the work arrangement ("Hybrid", "In-Office", "Distributed") is matched with the posting's office locations added. The screener applies these rules to digest listings before ranking, using the location text alone.

| Key | Purpose | Example |
|-----|---------|---------|
| `metro_cities` | A listing is kept if its location contains any of these names, as whole words, ignoring case: `"springfield"` matches "West Springfield, ST" but not "Springfieldtown". Names are plain text, so `"St. Louis"` works as written. Remote listings open to the US are kept regardless. An empty list matches every location. | `["springfield", "riverton"]` |
| `remote_regions` | Extra place names, such as your state and its abbreviation, that make a remote listing count as US-based when there's no country field. Matched as whole words, ignoring case. Without it, only US-wide wording ("US", "USA", "United States", "North America") or a bare "Remote" counts. Optional. | `["state name", "ST"]` |

## Board fetch (`board_fetch.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `pay_floor` | Annual base salary in USD. A posting is cut only when the **top** of its published range is below this; postings without published pay are kept. Pay is read from Greenhouse pay-transparency sections, and from Ashby and Lever salary data in USD, with hourly rates × 2080. SmartRecruiters postings never have pay parsed. Missing or `0` disables the cut. | `100000` |
| `included_title_patterns` | Python regular expressions. A board posting is fetched only if its title matches at least one, ignoring case. Matching is by substring, so `"analyst"` also matches "Analysts". An empty list fetches every title. Applies to board postings only. | `["analyst", "data ?engineer"]` |
| `excluded_keywords` | Plain phrases, checked after `included_title_patterns`. A board posting is skipped if its title contains one as a whole word, ignoring case: `"sales engineer"` blocks "Sales Engineer II" but not "Sales Platform Engineer". Applies to board postings only. | `["intern", "recruiter", "sales engineer"]` |

## Hard filters (`jobs_screen.py`, both sources)

| Key | Purpose | Example |
|-----|---------|---------|
| `excluded_employers` | Plain company names, matched as whole words, ignoring case: `"Acme"` cuts "Acme Corp" but not "Acmeworks". An empty list excludes nothing. | `["Acme", "Initech"]` |
| `excluded_title_patterns` | Python regular expressions, searched in the title, ignoring case. JSON needs doubled backslashes: write `"\\bintern\\b"` for `\bintern\b`. | `["\\bintern(ship)?\\b", "summer 20\\d\\d"]` |

## Digest parsing (`jobs_screen.py`)

| Key | Purpose | Example |
|-----|---------|---------|
| `digest_noise_lines` | Header or menu lines in your digest emails that can fall within the three lines above a `View` line and be misread as a listing. A candidate listing is dropped if its title, employer, or location equals one of these, ignoring case. Common table headers ("Job Title", "Employer", "Location", "Action") are handled already. Optional. | `["saved search name", "manage alerts"]` |

## Recency and ranking (`jobs_screen.py`)

| Key | Purpose | Default |
|-----|---------|---------|
| `recency_days` | Ingest leaves out digests that arrived more than this many days ago, plus one day, since its IMAP search counts arrival dates in whole days. The screener cuts digests older than this many days by their Date header. A listing still present in a newer digest stays. Board postings are exempt, because closed postings are archived instead. Digests with an unreadable date are kept, with a warning. `0` or missing disables the window. | `0` |
| `pass1_batch` | Listings per pass-1 request. Lower it only if responses come back truncated. | `30` |
| `rerank_top_n` | How many of the top pass-1 listings that have a description get re-scored in pass 2. `0` turns pass 2 off. | `15` |
| `rerank_batch` | Listings per pass-2 request. | `5` |
| `excerpt_chars` | Maximum length of the posting excerpt sent per listing in pass 2. | `1500` |
| `model_thinking` | Let a thinking model reason before it answers. See [Model thinking](#model-thinking) below. | `false` |
| `num_ctx` | The model's context window per request, in tokens. It must hold the prompt, the batch and the response. | `8192` |
| `score_caps` | Upper score limits applied in code after each pass, for example `[{"employers": ["Acme Staffing"], "max_score": 50}]`. Optional. See [Score caps](#score-caps). | `[]` |
| `shortlist_per_employer` | At most this many shortlist rows from one employer, for example `3`; the rest are counted under the table and stay in the JSON. `0` or missing means no limit. | `0` |

The pass-2 excerpt starts at the posting's role section: a heading such as "What you'll do", "Responsibilities" or "Job description". Failing that, it starts at a similar phrase in the first half of the description. Mentions inside closing EEO or pay text are ignored. It's cut at a sentence or line end.

### Model thinking

- **`false` (the default):**
  - About 18 s per pass-1 batch.
  - Scores are repeatable, because the first request is greedy.
  - The model follows the prompt's caps less reliably, which `score_caps` covers.
- **`true`:**
  - About 4× slower, and scores vary from run to run.
  - Long reasoning can overflow the context into empty or truncated responses, which are retried.
  - Greedy decoding isn't used, because the reasoning doesn't terminate.

## Score caps

`score_caps` is a list of rules. Each rule has a `max_score` and at least one of these:
- `employers`: plain names, matched like `excluded_employers` (whole words, ignoring case);
- `title_patterns`: regular expressions searched in the title;
- `description_patterns`: regular expressions searched in the stored posting text, so they apply to board postings only.

A rule matches a listing when any of its lists matches. A listing that matches several rules gets the lowest cap.

The screener stops at startup if a rule has no `max_score`, or none of the three lists. It also stops on an invalid regular expression, here or in `excluded_title_patterns`.

An optional `flag` names the rule in the shortlist's Flags column whenever it matches, even if the score was already under the cap. A `max_score` of 100 makes a rule a flag only.

Patterns ignore case and, like other regexes in JSON, need doubled backslashes.

```json
"score_caps": [
  {"employers": ["Not Available"], "max_score": 40},
  {"title_patterns": ["^(?!.*\\bsenior\\s*(/|or)\\s*(staff|principal)\\b).*\\b(staff|principal)\\b"], "max_score": 35},
  {"description_patterns": ["\\bts\\s*/\\s*sci\\b", "\\bsecurity clearance\\b"], "max_score": 40, "flag": "clearance"},
  {"title_patterns": ["\\bforward[- ]deploy(ed|ment)\\b"], "max_score": 100, "flag": "fde"}
]
```

The title rule caps staff and principal titles, but exempts multi-level postings such as "Senior / Staff Engineer".

A description pattern can't tell "clearance required" from "no clearance required". Check what a new pattern catches in `ranked_<date>.json`, in the `flags` field.

**Capping a skill only when it's required.** Posting text is one line per bullet, so a description pattern can look at a single line. To cap "required" mentions of a skill but not "nice to have" ones, match a line that:
- names the skill;
- states years or strong wording;
- doesn't say it's optional;
- for languages, isn't a list of alternatives.

The pattern below caps lines like "5+ years of Rails" or "strong Rails experience". It doesn't cap "Rails is a plus" or "one or more languages, e.g. Ruby, Java":

```json
{"description_patterns": ["(?:^|\\n)(?![^\\n]*(?:\\b(?:preferred|plus|nice[- ]to[- ]have|bonus)\\b|e\\.g\\.|such as|one or more))(?=[^\\n]*\\brails\\b)[^\\n]*\\b(?:\\d{1,2}\\s*\\+?\\s*(?:years?|yrs?)|required|strong|expert\\w*|proficien\\w*)\\b"], "max_score": 50, "flag": "rails"}
```

Bullets under a "Nice to have" heading still match, because a line can't see its heading. Check what a new rule catches with the `flags` field in `ranked_<date>.json`.

## Files

| Key | Purpose | Default |
|-----|---------|---------|
| `prompt_path` | System prompt for the ranking model (`jobs_screen.py`). The file must exist. | `"screening_prompt.txt"` |
| `applied_path` | Jobs you've applied to (see [Reviewing the shortlist](USAGE.md#reviewing-the-shortlist)). Optional; a missing file means none. | `"applied.txt"` |

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `EMAIL_USER` (or the name in `email_user_env`) | IMAP login. Required for ingest. | none |
| `EMAIL_PASSWORD` (or the name in `email_password_env`) | IMAP password. Required for ingest. | none |
| `OLLAMA_URL` | Ollama server address. | `http://localhost:11434` |
| `SCREEN_MODEL` | Ollama model tag used for ranking. | `gemma4:12b` |
| `EMAIL_IMAP_HOST`, `EMAIL_IMAP_PORT` | Fallbacks, used only when `imap_host` / `imap_port` are missing from the config. | none |
| `PYTHONUTF8` | Set to `1` on Windows so output redirected to log files is written as UTF-8. Set it in your scheduled-run wrappers, or once with `setx`. | not set |
