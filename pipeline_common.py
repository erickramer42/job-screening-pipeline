"""
pipeline_common.py — what the scripts share: loading
pipeline_config.json, the board-file description marker, email Date
parsing, and atomic file writes. Importing it reads no files.
"""
import json
import os
import re
import sys
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CONFIG_FILE = Path("pipeline_config.json")

# Board files store the posting description below this line, which
# board_fetch.py writes and jobs_screen.py stops quadruple parsing at,
# so description prose (which can contain "View" or anything else) is
# never read as a listing. Digest files have no marker.
DESC_MARKER = "--- DESCRIPTION ---"

def load_config():
    """pipeline_config.json as a dict. Fail closed: without it the run
    stops, rather than falling back to defaults."""
    if not CONFIG_FILE.exists():
        print("[ERROR] pipeline_config.json missing — copy "
              "pipeline_config.example.json and fill in your values.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

def configure_from_file(configure):
    """Load pipeline_config.json and pass it to a script's configure().
    A config mistake stops the run with one line naming the file, and
    exit code 1: a missing required key (KeyError), a bad value or JSON
    syntax (ValueError), or an invalid regular expression (re.error)."""
    try:
        configure(load_config())
        return
    except KeyError as e:
        print(f"[ERROR] {CONFIG_FILE} has no {e} key; it's required.")
    except re.error as e:
        print(f"[ERROR] {CONFIG_FILE}: bad regular expression ({e}) in "
              f"{e.pattern!r}")
    except ValueError as e:
        print(f"[ERROR] {CONFIG_FILE}: {e}")
    sys.exit(1)

def parse_email_date(value):
    """An email Date header as an aware datetime, taken as UTC when it
    names no zone; None when it's missing or unreadable."""
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def write_atomic(path, text):
    """Write text through a temporary file, so an interrupted run leaves
    the old file or the new one, never a truncated one. The .tmp name
    matches no glob the pipeline reads."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
