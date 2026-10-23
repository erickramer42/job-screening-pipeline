#!/usr/bin/env python3
"""
wrapper_helper.py — date and time decisions for the scheduled-run
wrappers. They're cmd scripts, where %date% and %time% follow the
locale, so the wrappers ask this script instead.

    python wrapper_helper.py begin 06:00
        Whether the morning's screening should start now. Exit 10 to
        skip: it's before 06:00, when the daily trigger runs it, or
        today's shortlist was already written at or after 06:00 (the
        task also starts at log on, after a restart that left no one
        signed in). Otherwise exit 0, and remove an earlier failure
        marker for today, since this run supersedes it. The wrapper
        skips only on exactly 10, so a crash here still runs the screen.

    python wrapper_helper.py failed RC ATTEMPTS LOG
        After the screener's last failed attempt, write
        out/FAILED_<date>.md: what exit code RC means, and what LOG
        shows for that attempt. out/ is where the owner looks each
        morning; the screener itself writes nothing when nothing was
        scored.
"""
import re
import sys
from datetime import datetime, time
from pathlib import Path

import jobs_screen
from pipeline_common import write_atomic

SKIP = 10
# The wrappers log a header line like "[<date> <time>] screen attempt 2"
ATTEMPT_RE = re.compile(r"\] \w[\w ]* attempt \d+\s*$")
# The lines that say what went wrong; a traceback ends in "...Error: ..."
PROBLEM_RE = re.compile(r"\[(ERROR|WARN)\]|FAILED|Traceback|\w+Error\b")

def shortlist_path(day):
    return jobs_screen.OUT_DIR / f"shortlist_{day.isoformat()}.md"

def marker_path(day):
    return jobs_screen.OUT_DIR / f"FAILED_{day.isoformat()}.md"

def skip_reason(start, now):
    """Why screening shouldn't start at now, or None when it should. A
    shortlist written before start (a manual run in the night) doesn't
    count: the later ingest may have added digests since."""
    since = datetime.combine(now.date(), start)
    if now < since:
        return f"before {start:%H:%M}, when the daily run starts"
    path = shortlist_path(now.date())
    if path.exists():
        written = datetime.fromtimestamp(path.stat().st_mtime)
        if written >= since:
            return f"{path} was already written at {written:%H:%M}"
    return None

def last_attempt(log_path):
    """The lines the wrapper's last attempt wrote to log_path."""
    path = Path(log_path)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    starts = [i for i, ln in enumerate(lines) if ATTEMPT_RE.search(ln)]
    return lines[starts[-1] + 1:] if starts else lines[-40:]

def meaning(rc, shortlist_written):
    """What the screener's exit code says about the morning."""
    if rc == 2:
        return ("Nothing was scored, so no shortlist was written. Usually "
                "Ollama wasn't running.")
    if rc == 1 and shortlist_written:
        return ("A partial run: the shortlist was written, and its first "
                "lines say what failed.")
    if rc == 1:
        return ("No shortlist was written, for a reason a retry can't fix: "
                "the config, a model that isn't pulled, a request Ollama "
                "rejected, or a crash. The log lines below say which.")
    return f"The screener stopped with exit code {rc}."

def write_failure(rc, attempts, log_path, now):
    """Write today's failure marker; returns its path."""
    lines = last_attempt(log_path)
    problems = [ln for ln in lines if PROBLEM_RE.search(ln)][-20:]
    text = [f"# Screening failed: {now.date().isoformat()}", "",
            f"The scheduled run exited {rc} at {now:%H:%M}, after "
            f"{attempts} attempt(s). "
            f"{meaning(rc, shortlist_path(now.date()).exists())}", "",
            f"From {log_path}, the last attempt:", ""]
    text += [f"    {ln}" for ln in problems or lines[-10:]] or ["    (empty)"]
    path = marker_path(now.date())
    path.parent.mkdir(exist_ok=True)
    write_atomic(path, "\n".join(text) + "\n")
    return path

def main(argv, now=None):
    now = now or datetime.now()
    if len(argv) == 2 and argv[0] == "begin":
        reason = skip_reason(time.fromisoformat(argv[1]), now)
        if reason:
            print(f"[{now:%Y-%m-%d %H:%M}] screen skipped: {reason}")
            return SKIP
        marker_path(now.date()).unlink(missing_ok=True)
        return 0
    if len(argv) == 4 and argv[0] == "failed":
        path = write_failure(int(argv[1]), int(argv[2]), argv[3], now)
        print(f"Wrote {path}")
        return 0
    print(__doc__)
    return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
