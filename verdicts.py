#!/usr/bin/env python3
"""
verdicts.py — the owner's verdicts on shortlist rows, read from the
feedback files, and a check of how a ranked run orders them.

jobs_screen.py writes feedback/<date>.md with one line per shortlist
row:   12 [?] Senior Backend Engineer | Acme ::
Replace ? with y (would apply), m (maybe) or n (no), and add a if you
applied: [m,a] is a maybe you applied to anyway, and [a] alone records
only the application. Put notes after "::". The row number is only a
convenience; the title and employer identify the listing, so verdicts
stay valid across runs. Lines in any other shape (free notes, headings)
are ignored; a row line whose flags can't be read is reported, not
skipped quietly.

A later line on the same listing updates the earlier verdict: its grade
and note replace the earlier ones unless they're empty, and "applied"
stays set once given. A board row is also matched by its posting URL,
looked up in the ranked JSON of the run its feedback file judges, so a
verdict survives an edit to the posting's title.

The title/employer split is at the LAST unescaped "|" (split_listing,
which jobs_screen.py uses for applied.txt too), so a pipe in a title is
fine, and the template puts a backslash before any pipe in an employer
name so it can't be mistaken for the separator.

The evaluation reads only the ranked JSON: no config, no model. Each
run is judged by the shortlist it showed (its rows' shortlist_rank);
runs written before that field existed showed their top 25.

    python verdicts.py                              # latest run in out/
    python verdicts.py out/ranked_2026-10-22.json

Verdicts that shaped a change (prompt, caps) score that change in-sample;
only later days' verdicts show whether it generalizes.
"""
import json
import re
import sys
from pathlib import Path

FEEDBACK_DIR = Path("feedback")
# The screener's real runs, where each feedback file's run is found by
# its date. Not jobs_screen.OUT_DIR: a scratch run points that elsewhere
# and still needs the real runs' URLs
RUNS_DIR = Path("out")
# A row line: number (or "-"), [flags], "title | employer", then an
# optional ":: note". The flags are read by parse_flags
LINE_RE = re.compile(r"^\s*(\d+|-)\.?\s*\[([^\]]*)\]\s*(.+?)\s*(?:::\s*(.*))?$")
# Ordering grades for the evaluation. "a" (applied) is a separate flag:
# applied listings are cut before ranking, so a later run can't be
# judged on where it put them
GRADES = {"y": 2, "m": 1, "n": 0}

def parse_flags(text):
    """(grade, applied) from a row's flags, e.g. "m,a" -> ("m", True);
    the grade is None for "a" alone. None for an unfilled row ("?" or
    blank). Raises ValueError for anything else: an unknown letter, or
    two different grades."""
    flags = {f.strip().lower() for f in text.split(",")} - {""}
    if not flags or flags == {"?"}:
        return None
    unknown = flags - set(GRADES) - {"a"}
    grades = flags & set(GRADES)
    if unknown or len(grades) > 1:
        raise ValueError(f"[{text}]")
    return (grades.pop() if grades else None), "a" in flags

def listing_key(title, employer):
    """A listing's identity: jobs_screen.py deduplicates, batches and
    cuts applied listings on it, and verdicts are stored under it."""
    return (title.strip().lower(), employer.strip().lower())

def split_listing(text):
    """(title, employer) from "Title | Employer", or None when there's
    no separator or a side is empty. Splits at the last unescaped "|",
    so a pipe in a title is fine; a "\\|" in the employer is a literal
    pipe, as the template writes it."""
    parts = re.split(r"(?<!\\)\|", text)
    if len(parts) < 2:
        return None
    title = "|".join(parts[:-1]).strip()
    employer = parts[-1].replace("\\|", "|").strip()
    return (title, employer) if title and employer else None

def run_urls(stem):
    """{listing key: posting URL} for the board rows of
    RUNS_DIR/ranked_<stem>.json, the run feedback/<stem>.md judges; {}
    when there's no such run. Digest rows have no URL."""
    path = RUNS_DIR / f"ranked_{stem}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {listing_key(j["title"], j["employer"]): j["url"]
            for j in data["ranked"] + data["rejected"] if j.get("url")}

def load_verdicts(folder=None):
    """{listing key: {"verdict", "applied", "note", "file", "url"}},
    where verdict is y, m, n or None (applied only), and url is the
    posting's link, or "" for a digest row or a run that's gone. Unfilled
    rows are skipped; a row line that can't be read is reported and
    skipped. Files are read in name (date) order, and a later line on a
    listing updates its verdict (see the module docstring). A line is
    matched to an earlier verdict by URL first, then by title and
    employer; the verdict is kept under the latest title."""
    folder = folder or FEEDBACK_DIR
    verdicts, key_by_url = {}, {}
    for f in sorted(folder.glob("*.md")) if folder.exists() else []:
        urls = run_urls(f.stem)
        for n, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = LINE_RE.match(ln)
            if not m:
                continue
            listing = split_listing(m.group(3))
            try:
                flags = parse_flags(m.group(2))
            except ValueError as e:
                print(f"[WARN] {f}:{n}: can't read the flags {e}; "
                      f"line ignored. Use y, m or n, plus a: [m,a]")
                continue
            if flags is None:
                continue
            if not listing:
                print(f"[WARN] {f}:{n}: no \"title | employer\" after the "
                      f"flags; line ignored")
                continue
            key = listing_key(*listing)
            url = urls.get(key, "")
            # The earlier verdict on this posting: found by URL, so a
            # board row whose title changed still has it, else by key
            earlier = key_by_url.get(url, key) if url else key
            old = (verdicts.get(key, {}) if earlier == key
                   else verdicts.pop(earlier, None) or {})
            grade, applied = flags
            v = {"verdict": grade if grade else old.get("verdict"),
                 "applied": applied or old.get("applied", False),
                 "note": (m.group(4) or "").strip() or old.get("note", ""),
                 "file": f.name,
                 "url": url or old.get("url", "")}
            verdicts[key] = v
            if v["url"]:
                key_by_url[v["url"]] = key
    return verdicts

def template_lines(stamp, rows):
    """The feedback file the screener writes for a day's shortlist."""
    lines = [f"# Feedback on out/shortlist_{stamp}.md",
             "# Per row, replace ? with y (would apply), m (maybe) or n (no),",
             "# and add a if you applied: [m,a]. Notes go after ::. Keep",
             "# \"title | employer\" as written: it identifies the listing.",
             "# Other lines are free notes.",
             ""]
    for i, j in enumerate(rows, 1):
        employer = j["employer"].replace("|", "\\|")
        lines.append(f"{i} [?] {j['title']} | {employer} ::")
    return lines

def ordering_agreement(graded):
    """(agreeing pairs, pairs) over the pairs of (grade, score) entries
    with different grades: a pair agrees when the better grade has the
    higher score, and an equal score counts half. Scores, not list
    positions: the screener breaks score ties in an arbitrary (hash)
    order, which says nothing about the model's judgment."""
    agree = total = 0
    for i, (g1, s1) in enumerate(graded):
        for g2, s2 in graded[i + 1:]:
            if g1 != g2:
                total += 1
                hi, lo = (s1, s2) if g1 > g2 else (s2, s1)
                agree += 1 if hi > lo else 0.5 if hi == lo else 0
    return agree, total

def evaluate(ranked_path, verdicts):
    """Print how a ranked run treats the listings that have verdicts."""
    data = json.loads(Path(ranked_path).read_text(encoding="utf-8"))
    ranked = data["ranked"]
    shown = [j for j in ranked if "shortlist_rank" in j] or ranked[:25]
    # Shortlist position, which the per-employer limit can put well
    # above the overall rank
    position = {listing_key(j["title"], j["employer"]):
                j.get("shortlist_rank", n) for n, j in enumerate(shown, 1)}
    rank = {listing_key(j["title"], j["employer"]): (i, j)
            for i, j in enumerate(ranked, 1)}
    last_shown = max((rank[k][0] for k in position), default=0)
    # Listings cut as applied are left out: that cut isn't the ranker's
    # judgment, so a "yes" you applied to shouldn't count as ranked last
    cut = {listing_key(j["title"], j["employer"]): j["reject_reason"]
           for j in data["rejected"] if j["reject_reason"] != "applied"}
    cut_score = -1  # a cut listing ranks below every ranked one
    # A board row is matched by its posting URL first, so a verdict
    # still finds a posting whose title has changed since
    row_by_url = {j["url"]: listing_key(j["title"], j["employer"])
                  for j in ranked + data["rejected"] if j.get("url")}

    seen = []  # (grade, score, the run's key, the verdict's key)
    for key, v in verdicts.items():
        row = row_by_url.get(v.get("url")) or key
        if v["verdict"] in GRADES and (row in rank or row in cut):
            seen.append((GRADES[v["verdict"]],
                         rank[row][1]["score"] if row in rank else cut_score,
                         row, key))
    applied = sum(v["applied"] for v in verdicts.values())
    print(f"{ranked_path}: {len(verdicts)} verdicts, {len(seen)} on listings "
          f"in this run ({applied} marked applied)")
    if not seen:
        return

    agree, total = ordering_agreement([(g, s) for g, s, _, _ in seen])
    if total:
        print(f"ordering agreement: {agree / total:.0%} of {total} pairs "
              f"(50% is random, 100% is your order)")
    for verdict, grade in GRADES.items():
        rows = [r for g, _, r, _ in seen if g == grade]
        if rows:
            print(f"  {verdict}: {sum(r in position for r in rows)} of "
                  f"{len(rows)} in the shortlist")

    def where(row):
        if row in cut:
            return f"cut ({cut[row]})"
        i, j = rank[row]
        if row in position:
            return f"shortlist #{position[row]}, score {j['score']}"
        # Ranked above the list's last row, so the list passed it over
        held = ", held out by the per-employer limit" if i < last_shown else ""
        return f"rank {i}, score {j['score']}{held}"
    misses = [(r, k) for g, _, r, k in seen if g == 2 and r not in position]
    false_hits = [(r, k) for g, _, r, k in seen if g == 0 and r in position]
    for label, pairs in (("yes, but not in the shortlist", misses),
                         ("no, but in the shortlist", false_hits)):
        if pairs:
            print(f"{label}:")
            for r, k in pairs:
                print(f"  - {r[0]} | {r[1]}: {where(r)} "
                      f"[{verdicts[k]['note'][:60]}]")

def main():
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        runs = sorted(Path("out").glob("ranked_????-??-??.json"))
        if not runs:
            print("No out/ranked_<date>.json yet; pass a file.")
            return 1
        path = runs[-1]
    evaluate(path, load_verdicts())
    return 0

if __name__ == "__main__":
    sys.exit(main())
