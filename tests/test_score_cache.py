"""Tests for score_cache.py: the request key, entry ages, and the file.
A stale or mis-keyed cache fails silently, by serving old scores, so
these pin what makes a key change. Run from the repo root with:
python -m unittest
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import score_cache

REQUEST = ("gemma4:12b", False, 8192, "Rank jobs.",
           "Score every job below.\n\nJobs:\n0: Engineer | Acme | Springfield")

class RequestKey(unittest.TestCase):
    def test_the_same_request_has_the_same_key(self):
        self.assertEqual(score_cache.request_key(*REQUEST),
                         score_cache.request_key(*REQUEST))

    def test_any_change_to_the_request_changes_the_key(self):
        base = score_cache.request_key(*REQUEST)
        model, thinking, num_ctx, system, user = REQUEST
        changed = {
            "model": (model + "-q8", thinking, num_ctx, system, user),
            "thinking": (model, True, num_ctx, system, user),
            "num_ctx": (model, thinking, 16384, system, user),
            "prompt": (model, thinking, num_ctx, system + " Be strict.", user),
            "title": (model, thinking, num_ctx, system,
                      user.replace("Engineer", "Senior Engineer")),
            "employer": (model, thinking, num_ctx, system,
                         user.replace("Acme", "Acme Labs")),
            "location": (model, thinking, num_ctx, system,
                         user.replace("Springfield", "Remote")),
            "title case": (model, thinking, num_ctx, system,
                           user.replace("Engineer", "engineer")),
            "excerpt": (model, thinking, num_ctx, system,
                        user + "\nPOSTING: Build things."),
        }
        for what, request in changed.items():
            with self.subTest(what):
                self.assertNotEqual(score_cache.request_key(*request), base)

    def test_fields_cant_run_into_each_other(self):
        # "ab" + "c" and "a" + "bc" are different requests
        self.assertNotEqual(
            score_cache.request_key("m", False, 1, "ab", "c"),
            score_cache.request_key("m", False, 1, "a", "bc"))

class Entries(unittest.TestCase):
    def test_lookup_marks_an_entry_used(self):
        entries = {}
        score_cache.store(entries, "k", 70, "fits", date(2026, 10, 1),
                          title="Engineer")
        self.assertEqual(entries["k"]["title"], "Engineer")
        self.assertIsNone(score_cache.lookup(entries, "other",
                                             date(2026, 10, 20)))
        entry = score_cache.lookup(entries, "k", date(2026, 10, 20))
        self.assertEqual((entry["score"], entry["scored"], entry["used"]),
                         (70, "2026-10-01", "2026-10-20"))

    def test_prune_drops_entries_unused_for_max_age_days(self):
        entries = {}
        for key, day in (("old", 1), ("edge", 5), ("new", 20)):
            score_cache.store(entries, key, 50, "", date(2026, 10, day))
        self.assertEqual(score_cache.prune(entries, date(2026, 10, 20), 15),
                         1)
        self.assertEqual(sorted(entries), ["edge", "new"])

    def test_prune_with_0_keeps_everything(self):
        entries = {}
        score_cache.store(entries, "k", 50, "", date(2020, 1, 1))
        self.assertEqual(score_cache.prune(entries, date(2026, 10, 20), 0), 0)
        self.assertEqual(list(entries), ["k"])

class CacheFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "out" / "score_cache.json"

    def test_a_missing_file_is_an_empty_cache(self):
        self.assertEqual(score_cache.load(self.path), {})

    def test_save_and_load_round_trip(self):
        entries = {}
        score_cache.store(entries, "k", 64, "fits", date(2026, 10, 25),
                          title="Engineer")
        with mock.patch("score_cache.write_atomic",
                        wraps=score_cache.write_atomic) as write:
            score_cache.save(self.path, entries)
        write.assert_called_once()
        self.assertEqual(score_cache.load(self.path), entries)
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         ["score_cache.json"])

    def test_an_unreadable_file_is_set_aside(self):
        self.path.parent.mkdir()
        for text in ('{"version": 1, "entries": {"k"', "[]",
                     json.dumps({"version": 0, "entries": {}}),
                     json.dumps({"version": 1, "entries": []})):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with mock.patch("builtins.print") as printed:
                    self.assertEqual(score_cache.load(self.path), {})
                self.assertIn("scored again", printed.call_args[0][0])
                self.assertFalse(self.path.exists())
                bad = self.path.with_name("score_cache.json.bad")
                self.assertEqual(bad.read_text(encoding="utf-8"), text)

if __name__ == "__main__":
    unittest.main()
