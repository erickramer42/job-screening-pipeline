#!/usr/bin/env python3
"""
shortlist.py — the screener's human-facing outputs: the daily shortlist
(out/shortlist_<date>.md) and the verdict template (feedback/<date>.md).

The shortlist has two sections, each ranked on its own: listings pass 2
read against their posting text, then "Needs lookup", scored on the
title alone. Rows are numbered on through both, as in the template, and
a per-employer limit spans both. jobs_screen.py decides which listings
go in each section and in what order; this module lays them out.
"""
import collections
from datetime import datetime

import verdicts
from pipeline_common import write_atomic

def md_cell(text, limit=None):
    """One Markdown table cell: whitespace collapsed (no stray line
    breaks), cut to limit, then pipes escaped. Escaping comes last so a
    cut can't leave a lone backslash that escapes the cell delimiter."""
    text = " ".join(str(text).split())
    if limit:
        text = text[:limit]
    return text.replace("|", r"\|")

def shortlist_rows(*sections, per_employer=0):
    """The top size rows of each (ranked, size) section, in order, with
    at most per_employer from any one employer (ignoring case; 0: no
    limit) across all of them. Also returns how many rows each employer
    had passed over for that limit before their section filled, under
    the employer's first spelling. A passed-over row is marked
    employer_limited, so verdicts.py can say why it's missing."""
    picked, shown, more = [], collections.Counter(), collections.Counter()
    names = {}
    for ranked, size in sections:
        rows = []
        for j in ranked:
            if len(rows) >= size:
                break
            emp = j["employer"].lower()
            names.setdefault(emp, j["employer"])
            if per_employer and shown[emp] >= per_employer:
                more[emp] += 1
                j["employer_limited"] = True
                continue
            shown[emp] += 1
            rows.append(j)
        picked.append(rows)
    return picked, collections.Counter({names[e]: n
                                        for e, n in more.items()})

def shortlist_table(rows, first):
    """A shortlist section's Markdown table lines, numbered from first,
    or a line saying the section is empty."""
    if not rows:
        return ["No listings.\n"]
    lines = ["| # | Score | Flags | Title | Employer | Location | Posted "
             "| Link | Reason |\n",
             "|---|-------|-------|-------|----------|----------|--------"
             "|------|--------|\n"]
    for i, j in enumerate(rows, first):
        mark = "▲ " if j.get("rescored") else ""
        lowered = "capped_from" in j or "verdict_capped_from" in j
        flags = j.get("flags", []) + (["capped"] if lowered else [])
        lines.append(f"| {i} | {mark}{j['score']} | {', '.join(flags)} | "
                     f"{md_cell(j['title'])} | {md_cell(j['employer'])} | "
                     f"{md_cell(j['location'])} | "
                     f"{md_cell(j.get('posted') or '-')} | "
                     f"{md_cell(j.get('url') or '-')} | "
                     f"{md_cell(j['reason'], 80)} |\n")
    return lines

def markdown(stamp, main_rows, lookup_rows, ranked_count, rejected, more,
             per_employer, notices):
    """The shortlist file's text. notices are the warnings that go at the
    top (a failed pass, a digest that parsed to nothing), one sentence
    each; more is shortlist_rows' count of rows the per-employer limit
    passed over; rejected supplies the jobs already applied to."""
    md = [f"# Daily Shortlist — {stamp}\n\n"]
    md += [f"**{n}**\n\n" for n in notices]
    md.append(f"{ranked_count} ranked, {len(rejected)} auto-cut. "
              f"Board postings link directly; digest rows need the "
              f"email. Rows marked ▲ were re-scored against the "
              f"posting text. Flags come from score_caps rules and your "
              f"verdicts; \"capped\" means a rule or a \"no\" verdict "
              f"lowered the model's score. Rows judged no aren't in the "
              f"verdict template; add a line there to change one.\n\n")
    md.append("## Read against the posting\n\n")
    md += shortlist_table(main_rows, 1)
    md.append("\n## Needs lookup\n\nScored on title, employer and location "
              "alone: digest rows, and board postings pass 2 didn't "
              "re-score. So they're ranked on their own. Open the posting "
              "before judging one.\n\n")
    md += shortlist_table(lookup_rows, len(main_rows) + 1)
    if more:
        md.append("\nMore from: "
                  + ", ".join(f"{md_cell(e)} (+{n})"
                              for e, n in more.most_common())
                  + f". At most {per_employer} per employer are "
                    f"shown across both tables; the rest are in the "
                    f"JSON.\n")
    applied = [j for j in rejected if j["reject_reason"] == "applied"]
    if applied:
        md.append("\n## Already applied, still posted\n\n")
        for j in applied:
            md.append(f"- {md_cell(j['title'])} · {md_cell(j['employer'])}"
                      f" · {md_cell(j['location'])} · "
                      f"{j.get('url') or 'digest listing'}\n")
    return "".join(md)

def write_verdict_template(path, stamp, rows):
    """The day's feedback file for the owner (verdicts.py). A new file
    gets the template. On a rerun the same day, a file that's still the
    untouched template is rewritten for this run's rows. Once the owner
    has filled anything in, only the rows new to the list are appended,
    under a comment: no line already there changes, line endings
    included. Returns what was done, for the log."""
    template = "\n".join(verdicts.template_lines(stamp, rows)) + "\n"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        write_atomic(path, template)
        return "written"
    with open(path, encoding="utf-8", newline="") as f:
        old = f.read()
    if verdicts.is_untouched_template(old, stamp):
        if old.replace("\r\n", "\n") == template:
            return "unchanged"
        write_atomic(path, template)
        return "rewritten for this run's rows, since none was filled in"
    listed = verdicts.listed_keys(old)
    added = [verdicts.template_row(n, j) for n, j in verdicts.numbered(rows)
             if verdicts.listing_key(j["title"], j["employer"]) not in listed]
    if not added:
        return "unchanged"
    eol = "\r\n" if "\r\n" in old else "\n"
    lead = "" if not old or old.endswith("\n") else eol
    write_atomic(path, old + lead + eol.join(
        ["", f"# Rerun at {datetime.now():%H:%M}: rows new to the shortlist",
         *added]) + eol, newline="")
    return f"{len(added)} row(s) new to the list appended"
