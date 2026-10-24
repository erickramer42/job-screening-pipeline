# Job Screening Pipeline

A daily pipeline that collects job listings from two sources and removes clear mismatches with deterministic rules. It ranks the rest with a local LLM and writes a 25-row shortlist each morning. It finds and ranks; a person reviews the list and applies.

It was built for one candidate's job search and has run on a daily schedule since October 2026. It's plain Python standard library plus a local model served by [Ollama](https://ollama.com). There are no third-party packages.

On a typical day:
- it parses about 360 unique listings and ranks about 280 of them;
- it finishes in under 5 minutes on a local 12B-parameter model.

## How it works

```
job-alert emails --(IMAP, read-only)--> job_ingest.py  --> raw/
career boards    --(public APIs)------> board_fetch.py --> raw_boards/
                                                              |
jobs_screen.py <----------------------------------------------+
  deduplicate -> filter -> LLM pass 1 -> LLM pass 2 -> out/shortlist_<date>.md
                                                              |
verdicts.py <-- the owner's verdicts in feedback/<date>.md <--+
```

1. **Ingest** reads job-alert digest emails from a dedicated mail folder over IMAP, read-only, and saves each one as text.
2. **Board fetch** pulls postings from the public Greenhouse, Lever, Ashby and SmartRecruiters APIs for a list of companies.
   - It keeps postings that match the title patterns, search area and pay floor.
   - It stores each one with its description, and archives postings that close.
3. **Screen:**
   - **Filter:** it merges the two sources and cuts duplicates and stale digests. It also cuts excluded employers and titles, listings outside the search area, and jobs already applied to.
   - **Pass 1:** the model scores every remaining listing 0–100 from its title, employer and location.
   - **Pass 2:** the top listings that have posting text are re-scored with an excerpt of the posting's role section.
   - **Hard limits:** after each pass, limits are enforced in code. For example, a posting that requires a skill the candidate lacks is capped at 50.
   - **Output:** it writes the shortlist (Markdown), the full ranking with every cut and its reason (JSON), and a verdict template. The shortlist ranks listings without posting text in their own "Needs lookup" section, since a title alone is weak evidence.
4. **Evaluate:** the owner marks shortlist rows yes, maybe, no or applied. `verdicts.py` then scores any run against those verdicts. A "no" holds that listing down on later shortlists.

## Sample output

The rows are placeholders, laid out in the real shortlist's format:

**With posting text**

| # | Score | Flags | Title | Employer | Location | Posted | Link | Reason |
|---|-------|-------|-------|----------|----------|--------|------|--------|
| 1 | ▲ 86 |  | Senior Backend Engineer | Acme Robotics | Springfield, ST ($150,000-$180,000) | 2026-10-20 | https://… | Python services and distributed systems at the core; level fits. |
| 2 | ▲ 50 | rails, capped | Senior Platform Engineer | Globex | Hybrid: Springfield, ST | 2026-08-03 | https://… | Strong platform fit, but requires 5+ years of Rails. |

**Needs lookup**

| # | Score | Flags | Title | Employer | Location | Posted | Link | Reason |
|---|-------|-------|-------|----------|----------|--------|------|--------|
| 3 | 72 |  | Software Engineer III | Initech | Remote - US | - | - | Backend title at the right level; stack unknown from the listing. |
| 4 | 50 | judged no, capped | Senior Cloud Engineer | Umbrella Corp | Springfield, ST | - | - | Senior title at a direct employer; cloud stack unclear. |

- ▲ marks a score revised against the posting text in pass 2.
- `rails` is a config rule's flag, and `capped` means that rule lowered the model's score. `judged no` marks a listing the owner ruled out on an earlier day, held at 50.
- Posted is the date the board published the posting. Digest rows have neither a date nor a link, so they always land under Needs lookup.

## Design decisions

- **Deterministic rules before the model.** Filters and score caps live in code and config, and the model handles judgment. Every cut listing keeps its reason in the JSON output.
- **A local model.** Listings and the candidate's profile never leave the machine, and a run costs nothing.
- **Read-only mail, enforced by the protocol.** The folder is opened with IMAP `EXAMINE` and messages are fetched with `BODY.PEEK[]`, so not even a bug can change the mailbox.
- **Fail closed.** A missing config, prompt, credential or mail folder stops the run. Nothing falls back to defaults, or to the inbox.
- **No scraping.** Data comes only from emails delivered to the mailbox and from the platforms' public posting APIs.
- **Private by default.** Personal settings live in gitignored files: the search area, employers, pay floor, mail account and ranking prompt. Credentials live only in environment variables. The repository holds placeholders only.
- **Standard library only.** There's nothing to install or keep patched.

## Problems found and fixed

Each of these was measured before and after the change. [STATUS.md](STATUS.md) has the details.

- **A thinking model whose answers came back empty.**
  - The default model reasons before it answers, and Ollama turns that on by default. The hidden reasoning filled the 8k-token context, which caused empty or truncated responses on 9 of 11 logged runs.
  - With thinking off and a greedy first attempt, a batch takes about 18 s instead of 60–90 s.
  - Scores on identical input now differ by 1.2 points on average, against 6.0 from one day to the next before.
  - The trade-off: the model then kept the prompt's hard limits less reliably, so those limits moved into code.
- **Untrusted model output.** Every response is validated before it's used:
  - 1-based numbering is detected and shifted, and ambiguous numbering is rejected rather than guessed.
  - A truncated response is salvaged only up to its last complete entry.
  - Retries keep the most complete answer.
- **Scores that depend on their batch-mates.**
  - Scored next to different listings, the same listing moves about 6 points on average.
  - Replacing a per-batch quota with an absolute rubric barely changed that (6.8 → 6.3 points).
  - The next step is to score each listing on its own, with a score cache so that only new listings cost a request.
- **Stored postings drifting from the live ones.** A read-only comparison found that 22% of stored postings had changed since they were saved. Board files now follow their posting, and every file is written atomically.
- **Unverified TLS.** Python's `imaplib` doesn't verify server certificates unless it's given an SSL context. Ingest always passes one, and accepts turning verification off only for a loopback host.
- **Evaluation that guards against fooling itself.**
  - Verdicts that shaped a change can only score it in-sample, so each change is judged on later days' verdicts.
  - The first out-of-sample check gave 64% pairwise ordering agreement (50% is random), against 84% in-sample.
  - Most of the misses were requirements stated in the posting text. That led to caps on required skills the candidate lacks.

## Reliability and testing

- **Unattended runs.** Windows Task Scheduler runs ingest overnight, then board fetch and screening at 06:00. The wrapper scripts retry and log.
  - Both tasks also start at log on, so an update restart that leaves no one signed in doesn't cost a morning. Screening still runs at most once a day.
  - A failed morning leaves `out/FAILED_<date>.md`, next to where the shortlist would be, with the reason.
- **An exit-code contract with the scheduler:**
  - The screener exits 2 only when nothing was scored and nothing was written, for example because Ollama wasn't running. That's the only case the wrapper retries.
  - A partial run, in either pass, still writes the shortlist, flagged at the top, and exits 1.
  - A preflight check tells an Ollama outage apart from a model that isn't installed, and a request Ollama rejects stops the run with its reason instead of being retried.
- **Atomic writes.** Every file is written through a temporary file, so a killed run never leaves a truncated one.
- **143 `unittest` tests**, which need no network and no personal config. They include the contracts between stages:
  - what ingest and board fetch write must parse in the screener;
  - the board-file layout is pinned byte for byte;
  - each exit code, with the screener run end to end against a stand-in model.

## Quick start

You'll need Python 3 (developed on 3.13) and Ollama.

```bash
cp pipeline_config.example.json pipeline_config.json   # then fill in your values
cp boards.example.json boards.json                     # the companies to fetch
# write the ranking prompt to screening_prompt.txt
ollama pull gemma4:12b

python board_fetch.py     # fetch board postings into raw_boards/
python jobs_screen.py     # rank, and write out/shortlist_<date>.md
python -m unittest        # run the tests
```

Email ingest (`python job_ingest.py`) also needs IMAP credentials in environment variables. [docs/USAGE.md](docs/USAGE.md) covers ingest, scheduling, the outputs, and the review loop.

## Repository layout

| Path | Role |
|------|------|
| `job_ingest.py` | Reads job-alert emails over IMAP, read-only |
| `board_fetch.py` | Fetches postings from the public board APIs |
| `jobs_screen.py` | Parsing, filters, two-pass LLM ranking, and the shortlist |
| `verdicts.py` | Reads the owner's verdicts and scores a run against them |
| `html_text.py`, `location_filter.py`, `pipeline_common.py` | Shared code: HTML to text, the search-area rule, config loading and atomic writes |
| `slug_probe.py` | Finds a company's board on the four platforms |
| `run_*.bat.example`, `wrapper_helper.py` | The scheduled-run wrappers, and the date decisions they leave to Python |
| `tests/` | The `unittest` suite |
| `*.example.json` | Placeholder config to start from |

## Documentation

- [docs/USAGE.md](docs/USAGE.md): setup, running, outputs, failure behavior, the review loop, scheduling, and writing the ranking prompt.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): every setting in `pipeline_config.json`, and the environment variables.
- [STATUS.md](STATUS.md): current state, the change history with measurements, known issues, and the roadmap.
