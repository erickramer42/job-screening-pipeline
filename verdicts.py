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
skipped quietly. A later file's verdict on the same listing replaces an
earlier one. The
title/employer split is at the LAST unescaped "|" (split_listing, which
jobs_screen.py uses for applied.txt too), so a pipe in a title is fine,
and the template puts a backslash before any pipe in an employer name
so it can't be mistaken for the separator.

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

def load_verdicts(folder=None):
    """{listing key: {"verdict", "applied", "note", "file"}}, where
    verdict is y, m, n or None (applied only). Unfilled rows are
    skipped; a row line that can't be read is reported and skipped.
    Files are read in name (date) order."""
    folder = folder or FEEDBACK_DIR
    verdicts = {}
    for f in sorted(folder.glob("*.md")) if folder.exists() else []:
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
            verdicts[listing_key(*listing)] = {
                "verdict": flags[0],
                "applied": flags[1],
                "note": (m.group(4) or "").strip(),
                "file": f.name,
            }
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
    in_list = {listing_key(j["title"], j["employer"]) for j in shown}
    rank = {listing_key(j["title"], j["employer"]): (i, j)
            for i, j in enumerate(ranked, 1)}
    # Listings cut as applied are left out: that cut isn't the ranker's
    # judgment, so a "yes" you applied to shouldn't count as ranked last
    cut = {listing_key(j["title"], j["employer"]): j["reject_reason"]
           for j in data["rejected"] if j["reject_reason"] != "applied"}
    cut_score = -1  # a cut listing ranks below every ranked one

    seen = []  # (grade, score, key)
    for key, v in verdicts.items():
        if v["verdict"] in GRADES and (key in rank or key in cut):
            seen.append((GRADES[v["verdict"]],
                         rank[key][1]["score"] if key in rank else cut_score,
                         key))
    applied = sum(v["applied"] for v in verdicts.values())
    print(f"{ranked_path}: {len(verdicts)} verdicts, {len(seen)} on listings "
          f"in this run ({applied} marked applied)")
    if not seen:
        return

    agree, total = ordering_agreement([(g, s) for g, s, _ in seen])
    if total:
        print(f"ordering agreement: {agree / total:.0%} of {total} pairs "
              f"(50% is random, 100% is your order)")
    for verdict, grade in GRADES.items():
        keys = [k for g, _, k in seen if g == grade]
        if keys:
            print(f"  {verdict}: {sum(k in in_list for k in keys)} of "
                  f"{len(keys)} in the shortlist")

    def where(key):
        if key in cut:
            return f"cut ({cut[key]})"
        i, j = rank[key]
        return f"rank {i}, score {j['score']}"
    misses = [k for g, _, k in seen if g == 2 and k not in in_list]
    false_hits = [k for g, _, k in seen if g == 0 and k in in_list]
    for label, keys in (("yes, but not in the shortlist", misses),
                        ("no, but in the shortlist", false_hits)):
        if keys:
            print(f"{label}:")
            for k in keys:
                print(f"  - {k[0]} | {k[1]}: {where(k)} "
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
