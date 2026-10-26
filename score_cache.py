#!/usr/bin/env python3
"""
score_cache.py — model scores kept between screening runs.

With per-listing scoring each listing is its own request, so its score
depends only on that request: the model, its settings, the prompt and
the request text (the listing line, plus the posting excerpt in pass 2).
An entry is keyed by a hash of all of that, so anything that would
change what the model sees changes the key, and the listing is scored
again. Nothing has to remember to invalidate it.

Entries hold the model's own score, before score_caps, which the
screener applies on read: a cap change takes effect without a re-score.
An entry no run has used for max_age_days is dropped when the cache is
saved, so the file holds roughly the listings still being screened.
"""
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from pipeline_common import write_atomic

# Bump when an entry's meaning changes; a file with another version is
# set aside and the cache starts empty
VERSION = 1

def request_key(model, thinking, num_ctx, system, user):
    """The cache key of one chat request: everything that decides the
    model's answer, hashed."""
    text = json.dumps([model, thinking, num_ctx, system, user])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def load(path):
    """{key: entry} from path; {} for a missing file. A file that isn't
    a cache of this VERSION is renamed to <name>.bad, with a warning, and
    the run starts from an empty cache: it's derived data, so the cost is
    a full re-score, never a wrong score."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION \
                or not isinstance(data.get("entries"), dict):
            raise ValueError(f"not a version {VERSION} score cache")
        return data["entries"]
    except (ValueError, AttributeError) as e:
        bad = path.with_name(path.name + ".bad")
        path.replace(bad)
        print(f"[WARN] {path} is unreadable ({e}); moved to {bad.name}, "
              f"and every listing will be scored again.")
        return {}

def lookup(entries, key, today):
    """The entry for key, marked used today; None when there isn't one."""
    entry = entries.get(key)
    if entry is not None:
        entry["used"] = today.isoformat()
    return entry

def store(entries, key, score, reason, today, **about):
    """Record a model score. about holds readable fields for whoever
    opens the file (pass, title, ...); only key decides a hit."""
    entries[key] = {"score": score, "reason": reason,
                    "scored": today.isoformat(), "used": today.isoformat(),
                    **about}

def prune(entries, today, max_age_days):
    """Drop the entries last used more than max_age_days before today
    (0: keep everything). Returns how many were dropped."""
    if not max_age_days:
        return 0
    oldest = (today - timedelta(days=max_age_days)).isoformat()
    old = [k for k, e in entries.items() if e.get("used", "") < oldest]
    for k in old:
        del entries[k]
    return len(old)

def save(path, entries):
    """Write the cache through write_atomic: a run killed mid-write
    leaves the previous file or the complete new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps({"version": VERSION, "entries": entries},
                                  indent=1, sort_keys=True))
