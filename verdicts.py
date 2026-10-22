#!/usr/bin/env python3
"""
verdicts.py — the owner's verdicts on shortlist rows, read from the
feedback files, and a check of how a ranked run orders them.

jobs_screen.py writes feedback/<date>.md with one line per shortlist
row:   12 [?] Senior Backend Engineer | Acme ::
Replace ? with y (would apply), m (maybe), n (no) or a (applied) and
put notes after "::". The row number is only a convenience; the title
and employer identify the listing, so verdicts stay valid across runs.
Lines in any other shape (free notes, headings) are ignored, and a
later file's verdict on the same listing replaces an earlier one. The
title/employer split is at the LAST " | ", so a pipe in a title is fine,
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
LINE_RE = re.compile(r"^\s*(\d+|-)\.?\s*\[([ymna?])\]\s*(.+?)\s*(?:::\s*(.*))?$",
                     re.I)
# Ordering grades for the evaluation; "a" (applied) listings are cut
# before ranking, so they have no rank to judge
GRADES = {"y": 2, "m": 1, "n": 0}

def listing_key(title, employer):
    """Same identity jobs_screen.py deduplicates listings on."""
    return (title.strip().lower(), employer.strip().lower())

def load_verdicts(folder=None):
    """{listing key: {"verdict", "note", "file"}}; unfilled rows (?) are
    skipped. Files are read in name (date) order."""
    folder = folder or FEEDBACK_DIR
    verdicts = {}
    for f in sorted(folder.glob("*.md")) if folder.exists() else []:
        for ln in f.read_text(encoding="utf-8").splitlines():
            m = LINE_RE.match(ln)
            if not m or m.group(2) == "?" or " | " not in m.group(3):
                continue
            title, employer = m.group(3).rsplit(" | ", 1)
            employer = employer.replace("\\|", "|")
            verdicts[listing_key(title, employer)] = {
                "verdict": m.group(2).lower(),
                "note": (m.group(4) or "").strip(),
                "file": f.name,
            }
    return verdicts

def template_lines(stamp, rows):
    """The feedback file the screener writes for a day's shortlist."""
    lines = [f"# Feedback on out/shortlist_{stamp}.md",
             "# Per row, replace ? with y (would apply), m (maybe), n (no) or",
             "# a (applied), and add notes after ::. Keep \"title | employer\"",
             "# as written: it identifies the listing. Other lines are free notes.",
             ""]
    for i, j in enumerate(rows, 1):
        employer = j["employer"].replace("|", "\\|")
        lines.append(f"{i} [?] {j['title']} | {employer} ::")
    return lines

def evaluate(ranked_path, verdicts):
    """Print how a ranked run treats the listings that have verdicts."""
    data = json.loads(Path(ranked_path).read_text(encoding="utf-8"))
    ranked = data["ranked"]
    shown = [j for j in ranked if "shortlist_rank" in j] or ranked[:25]
    in_list = {listing_key(j["title"], j["employer"]) for j in shown}
    rank = {listing_key(j["title"], j["employer"]): (i, j)
            for i, j in enumerate(ranked, 1)}
    cut = {listing_key(j["title"], j["employer"]): j["reject_reason"]
           for j in data["rejected"]}
    bottom = len(ranked) + 1  # a cut listing ranks below every ranked one

    seen = []  # (grade, rank, key)
    for key, v in verdicts.items():
        if v["verdict"] in GRADES and (key in rank or key in cut):
            seen.append((GRADES[v["verdict"]],
                         rank[key][0] if key in rank else bottom, key))
    applied = sum(v["verdict"] == "a" for v in verdicts.values())
    print(f"{ranked_path}: {len(verdicts)} verdicts, {len(seen)} on listings "
          f"in this run ({applied} applied, not ranked)")
    if not seen:
        return

    # Ordering agreement: over pairs with different verdicts, the share
    # where the better verdict ranks higher (ties count half)
    agree = total = 0
    for i, (g1, r1, _) in enumerate(seen):
        for g2, r2, _ in seen[i + 1:]:
            if g1 != g2:
                total += 1
                hi, lo = (r1, r2) if g1 > g2 else (r2, r1)
                agree += 1 if hi < lo else 0.5 if hi == lo else 0
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
