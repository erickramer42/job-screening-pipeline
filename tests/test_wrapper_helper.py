"""Tests for wrapper_helper.py: when the morning screen starts, and the
failure marker the wrapper writes to out/. Files go to a temporary
folder."""
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import jobs_screen as js
import wrapper_helper as wh

DAY = datetime(2026, 1, 2, 9, 30)

class Helper(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.out.mkdir()
        patcher = mock.patch.object(js, "OUT_DIR", self.out)
        patcher.start()
        self.addCleanup(patcher.stop)

    def shortlist(self, written):
        path = self.out / "shortlist_2026-01-02.md"
        path.write_text("# Daily Shortlist\n", encoding="utf-8")
        os.utime(path, (written.timestamp(), written.timestamp()))

class Begin(Helper):
    def begin(self, now):
        with mock.patch("builtins.print"):
            return wh.main(["begin", "06:00"], now)

    def test_runs_after_the_start_time_without_a_shortlist(self):
        self.assertEqual(self.begin(DAY), 0)

    def test_waits_for_the_daily_run_before_the_start_time(self):
        self.assertEqual(self.begin(datetime(2026, 1, 2, 5, 45)), wh.SKIP)

    def test_skips_a_day_whose_shortlist_is_written(self):
        self.shortlist(datetime(2026, 1, 2, 6, 5))
        self.assertEqual(self.begin(DAY), wh.SKIP)

    def test_a_shortlist_from_the_night_doesnt_count(self):
        # The 05:00 ingest may have added digests since
        self.shortlist(datetime(2026, 1, 2, 3, 0))
        self.assertEqual(self.begin(DAY), 0)

    def test_a_run_that_starts_clears_the_days_failure_marker(self):
        marker = self.out / "FAILED_2026-01-02.md"
        marker.write_text("failed\n", encoding="utf-8")
        self.assertEqual(self.begin(DAY), 0)
        self.assertFalse(marker.exists())

class Failed(Helper):
    def log(self, text):
        path = self.root / "screen.log"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_marker_explains_the_code_and_quotes_the_last_attempt(self):
        log = self.log(
            "[Fri 01/02/2026  6:00:01.00] screen attempt 1\n"
            "[ERROR] an earlier attempt's error\n"
            "[Fri 01/02/2026  6:15:01.00] screen attempt 2\n"
            "Pass 1: scoring 4 listings\n"
            "  batch 1 FAILED: timed out\n"
            "[ERROR] No batch scored. No shortlist written.\n")
        with mock.patch("builtins.print"):
            self.assertEqual(wh.main(["failed", "2", "2", str(log)], DAY), 0)
        text = (self.out / "FAILED_2026-01-02.md").read_text(encoding="utf-8")
        self.assertIn("exited 2 at 09:30, after 2 attempt(s)", text)
        self.assertIn("no shortlist was written", text)
        self.assertIn("    [ERROR] No batch scored.", text)
        self.assertIn("batch 1 FAILED: timed out", text)
        self.assertNotIn("earlier attempt", text)
        self.assertNotIn("Pass 1", text)

    def test_a_partial_run_says_the_shortlist_was_written(self):
        self.shortlist(DAY)
        path = wh.write_failure(1, 1, self.log("Traceback\nKeyError: x\n"),
                                DAY)
        text = path.read_text(encoding="utf-8")
        self.assertIn("A partial run", text)
        self.assertIn("KeyError: x", text)

    def test_a_missing_log_still_writes_the_marker(self):
        path = wh.write_failure(1, 1, self.root / "none.log", DAY)
        self.assertIn("(empty)", path.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
