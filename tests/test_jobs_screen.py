"""Tests for jobs_screen.py: listing identity (dedup, applied, batch
order), score caps, and the shortlist's per-employer limit. Run from the
repo root with: python -m unittest

Fixtures are written to a temporary folder; no personal config is read.
"""
import hashlib
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

import jobs_screen as js

CONFIG = {
    "excluded_employers": ["Excluded Co"],
    "excluded_title_patterns": [r"\bintern\b"],
    "metro_cities": ["springfield"],
    "remote_regions": ["ST"],
    "recency_days": 30,
    "digest_noise_lines": ["Manage alerts"],
    "score_caps": [
        {"employers": ["Acme Staffing"], "max_score": 50},
        {"title_patterns": [r"\bstaff\b"], "max_score": 35, "flag": "staff"},
        {"description_patterns": [r"security clearance"], "max_score": 40,
         "flag": "clearance"},
    ],
}

def digest_text(days_old, *listings):
    """A digest file: metadata with a Date header, then quadruples."""
    sent = datetime.now(timezone.utc) - timedelta(days=days_old)
    lines = ["# Metadata", "Subject: Job alerts",
             f"Date: {format_datetime(sent)}", "UID: 1", "---", ""]
    for title, employer, location in listings:
        lines += [title, employer, location, "View"]
    return "\n".join(lines) + "\n"

def board_text(title, employer, location, url, description=""):
    """A board file in board_fetch.py's layout."""
    lines = ["# Metadata", "Source: greenhouse/example",
             f"Company: {employer}", f"URL: {url}", f"Title: {title}",
             "Format: 2", "---", "", title, employer, location, "View"]
    text = "\n".join(lines) + "\n"
    if description:
        text += f"\n{js.DESC_MARKER}\n{description}\n"
    return text

class SourceFiles(unittest.TestCase):
    """Each test gets empty raw/ and raw_boards/ folders."""
    def setUp(self):
        js.configure(CONFIG)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = self.root / "raw"
        self.boards = self.root / "raw_boards"
        self.raw.mkdir()
        self.boards.mkdir()
        patcher = mock.patch.object(js, "SOURCE_DIRS", [
            (self.raw, "job_*.txt"), (self.boards, "*.txt")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, folder, name, text):
        (folder / name).write_text(text, encoding="utf-8")

class ListingIdentity(SourceFiles):
    def test_board_copy_wins_over_digest_copy(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Senior Engineer", "Acme", "Springfield")))
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "senior engineer", "ACME", "Springfield ($150,000-$180,000)",
            "https://example.com/1", "About the role\nBuild things."))
        jobs = js.parse_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], "raw_boards")
        self.assertEqual(jobs[0]["url"], "https://example.com/1")
        self.assertIn("Build things.", jobs[0]["description"])

    def test_newest_digest_copy_is_kept_and_judged_for_staleness(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            60, ("Senior Engineer", "Acme", "Springfield"),
            ("Old Role", "Acme", "Springfield")))
        self.write(self.raw, "job_uid_2.txt", digest_text(
            2, ("Senior Engineer", "Acme", "Springfield")))
        jobs = {j["title"]: j for j in js.parse_jobs()}
        self.assertEqual(jobs["Senior Engineer"]["source_file"],
                         "job_uid_2.txt")
        self.assertFalse(jobs["Senior Engineer"]["_stale"])
        self.assertTrue(jobs["Old Role"]["_stale"])

    def test_description_text_never_parses_as_a_listing(self):
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "Senior Engineer", "Acme", "Springfield", "https://example.com/1",
            "Fake Title\nFake Co\nSpringfield\nView"))
        self.assertEqual([j["title"] for j in js.parse_jobs()],
                         ["Senior Engineer"])

    def test_noise_lines_are_not_listings(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Manage alerts", "Acme", "Springfield"),
            ("Senior Engineer", "Acme", "Springfield")))
        self.assertEqual([j["title"] for j in js.parse_jobs()],
                         ["Senior Engineer"])

    def test_applied_by_url_and_by_title_with_a_pipe(self):
        applied = self.root / "applied.txt"
        applied.write_text(
            "# jobs applied to\n"
            "https://example.com/1#apply   # board row\n"
            "Lead | Platform | Acme  # digest row; the title has a pipe\n",
            encoding="utf-8")
        urls, pairs = js.load_applied(applied)
        # "#" starts a comment only after whitespace, so the fragment stays
        self.assertEqual(urls, {"https://example.com/1#apply"})
        rows = [
            {"title": "Senior Engineer", "employer": "Beta",
             "url": "https://example.com/1#apply", "source": "raw_boards"},
            {"title": "Lead | Platform", "employer": "ACME", "url": "",
             "source": "raw", "location": "Springfield"},
            {"title": "Lead", "employer": "Acme", "url": "",
             "source": "raw", "location": "Springfield"},
        ]
        with mock.patch.object(js, "APPLIED_URLS", urls), \
                mock.patch.object(js, "APPLIED_PAIRS", pairs):
            kept, rejected = js.hard_filter(rows)
        self.assertEqual([j["title"] for j in kept], ["Lead"])
        self.assertEqual({j["reject_reason"] for j in rejected}, {"applied"})

    def test_hard_filters(self):
        rows = [
            {"title": "Senior Engineer", "employer": "Excluded Co Inc",
             "url": "", "source": "raw", "location": "Springfield"},
            {"title": "Software Intern", "employer": "Acme",
             "url": "", "source": "raw", "location": "Springfield"},
            {"title": "Senior Engineer", "employer": "Acme",
             "url": "", "source": "raw", "location": "Elsewhere"},
            {"title": "Senior Engineer", "employer": "Acme",
             "url": "", "source": "raw", "location": "Remote - ST"},
        ]
        with mock.patch.object(js, "APPLIED_URLS", set()), \
                mock.patch.object(js, "APPLIED_PAIRS", set()):
            kept, rejected = js.hard_filter(rows)
        self.assertEqual([j["location"] for j in kept], ["Remote - ST"])
        self.assertEqual([j["reject_reason"] for j in rejected],
                         ["excluded employer", "excluded title pattern",
                          "outside area"])

    def test_batch_order_hash_is_pinned(self):
        # Batches are cut in this order, and a listing's score depends
        # on its batch-mates: a change here would reshuffle every batch
        job = {"title": "Senior Engineer", "employer": "Acme"}
        self.assertEqual(js.batch_order(job), hashlib.sha1(
            b"senior engineer|acme").hexdigest())

class ScoreCaps(unittest.TestCase):
    def setUp(self):
        js.configure(CONFIG)

    def job(self, title="Senior Engineer", employer="Acme",
            description=""):
        j = {"title": title, "employer": employer,
             "description": description}
        js.attach_caps([j])
        return j

    def test_lowest_matching_cap_wins_and_flags_combine(self):
        j = self.job("Staff Engineer", "Acme Staffing",
                     "A security clearance is required.")
        self.assertEqual(j["score_cap"], 35)
        self.assertEqual(j["flags"], ["clearance", "staff"])

    def test_employer_names_match_whole_words_only(self):
        self.assertNotIn("score_cap", self.job(employer="Acme Staffingworks"))
        self.assertEqual(self.job(employer="ACME STAFFING LLC")["score_cap"],
                         50)

    def test_no_match_no_cap(self):
        j = self.job()
        self.assertNotIn("score_cap", j)
        self.assertNotIn("flags", j)

    def test_set_score_clamps_and_keeps_the_model_score(self):
        j = self.job(employer="Acme Staffing")
        js.set_score(j, 72, "good")
        self.assertEqual((j["score"], j["capped_from"]), (50, 72))
        js.set_score(j, 30, "weaker")
        self.assertEqual(j["score"], 30)
        self.assertNotIn("capped_from", j)

    def test_rerank_keeps_the_pass1_score(self):
        batch = [self.job(employer="Acme Staffing")]
        js.apply_pass1(batch, {0: {"score": 45, "reason": "pass 1"}})
        js.apply_rerank(batch, {0: {"score": 80, "reason": "pass 2"}})
        j = batch[0]
        self.assertEqual((j["pass1_score"], j["score"], j["capped_from"]),
                         (45, 50, 80))
        self.assertTrue(j["rescored"])

class Configure(unittest.TestCase):
    def tearDown(self):
        js.configure(CONFIG)

    def test_rule_without_a_matcher_is_rejected(self):
        with self.assertRaises(ValueError):
            js.configure({**CONFIG, "score_caps": [{"max_score": 40}]})

    def test_rule_without_max_score_is_rejected(self):
        with self.assertRaises(ValueError):
            js.configure({**CONFIG, "score_caps": [{"employers": ["Acme"]}]})

    def test_bad_title_pattern_fails_at_startup(self):
        with self.assertRaises(re.error):
            js.configure({**CONFIG, "excluded_title_patterns": ["("]})

    def test_missing_required_key(self):
        cfg = dict(CONFIG)
        del cfg["metro_cities"]
        with self.assertRaises(KeyError):
            js.configure(cfg)

class Shortlist(unittest.TestCase):
    def tearDown(self):
        js.configure(CONFIG)

    def test_per_employer_limit_ignores_case(self):
        js.configure({**CONFIG, "shortlist_per_employer": 1})
        ranked = [{"title": f"Role {i}", "employer": e}
                  for i, e in enumerate(["Acme", "ACME", "Beta", "acme"])]
        rows, more = js.shortlist_rows(ranked)
        self.assertEqual([j["title"] for j in rows], ["Role 0", "Role 2"])
        self.assertEqual(dict(more), {"Acme": 2})

if __name__ == "__main__":
    unittest.main()
