"""Tests for jobs_screen.py: listing identity (dedup, applied, batch
order), score caps, model requests and responses, the shortlist's
per-employer limit, and main()'s exit codes. Run from the repo root
with: python -m unittest

Fixtures are written to a temporary folder; no personal config is read,
and a stand-in replaces the model.
"""
import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

import board_fetch
import jobs_screen as js
import pipeline_common

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

def board_text(title, employer, location, url, description="", posted=""):
    """A board file, as board_fetch.py writes it."""
    return board_fetch.record_text("greenhouse", "example", employer, url,
                                   posted, title, location, description)

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
        jobs, _ = js.parse_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], "raw_boards")
        self.assertEqual(jobs[0]["url"], "https://example.com/1")
        self.assertIn("Build things.", jobs[0]["description"])

    def test_a_board_row_carries_its_posted_date(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Data Engineer", "Beta", "Springfield")))
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "Senior Engineer", "Acme", "Springfield", "https://example.com/1",
            posted="2026-09-01"))
        jobs = {j["title"]: j for j in js.parse_jobs()[0]}
        self.assertEqual(jobs["Senior Engineer"]["posted"], "2026-09-01")
        self.assertNotIn("posted", jobs["Data Engineer"])

    def test_newest_digest_copy_is_kept_and_judged_for_staleness(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            60, ("Senior Engineer", "Acme", "Springfield"),
            ("Old Role", "Acme", "Springfield")))
        self.write(self.raw, "job_uid_2.txt", digest_text(
            2, ("Senior Engineer", "Acme", "Springfield")))
        jobs = {j["title"]: j for j in js.parse_jobs()[0]}
        self.assertEqual(jobs["Senior Engineer"]["source_file"],
                         "job_uid_2.txt")
        self.assertFalse(jobs["Senior Engineer"]["_stale"])
        self.assertTrue(jobs["Old Role"]["_stale"])

    def test_description_text_never_parses_as_a_listing(self):
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "Senior Engineer", "Acme", "Springfield", "https://example.com/1",
            "Fake Title\nFake Co\nSpringfield\nView"))
        self.assertEqual([j["title"] for j in js.parse_jobs()[0]],
                         ["Senior Engineer"])

    def test_noise_lines_are_not_listings(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Manage alerts", "Acme", "Springfield"),
            ("Senior Engineer", "Acme", "Springfield")))
        self.assertEqual([j["title"] for j in js.parse_jobs()[0]],
                         ["Senior Engineer"])

    def test_a_recent_digest_without_listings_is_reported(self):
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Senior Engineer", "Acme", "Springfield")))
        # A new layout: no View lines, so nothing parses
        self.write(self.raw, "job_uid_2.txt", digest_text(1)
                   + "Senior Engineer\nAcme\nSpringfield\nApply now\n")
        # Past the recency window, and saved without a text part
        self.write(self.raw, "job_uid_3.txt", digest_text(60) + "Old text\n")
        self.write(self.raw, "job_uid_4.txt", digest_text(1))
        jobs, unparsed = js.parse_jobs()
        self.assertEqual([j["title"] for j in jobs], ["Senior Engineer"])
        self.assertEqual(unparsed, ["job_uid_2.txt"])

    def test_metadata_comes_from_the_block_only(self):
        lines = ["# Metadata", "URL: https://example.com/1", "Date: x",
                 "URL: https://example.com/2", "---",
                 "URL: https://elsewhere.example.com"]
        self.assertEqual(js.metadata(lines),
                         {"URL": "https://example.com/1", "Date": "x"})
        self.assertEqual(js.metadata(["Senior Engineer", "URL: x"]), {})

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

    def test_applied_flag_in_a_feedback_file_cuts_the_listing(self):
        feedback = self.root / "feedback"
        feedback.mkdir()
        (feedback / "2026-01-01.md").write_text(
            "1 [m,a] Senior Engineer | Acme ::\n", encoding="utf-8")
        prompt = self.root / "prompt.txt"
        prompt.write_text("Rank jobs.", encoding="utf-8")
        js.configure({**CONFIG, "prompt_path": str(prompt),
                      "applied_path": str(self.root / "none.txt")})
        with mock.patch.object(js.verdicts, "FEEDBACK_DIR", feedback), \
                mock.patch.object(js.verdicts, "RUNS_DIR", self.root):
            js.load_inputs()
        self.assertIn(("senior engineer", "acme"), js.APPLIED_PAIRS)

    def test_an_applied_board_row_stays_cut_after_a_title_edit(self):
        feedback, runs = self.root / "feedback", self.root / "out"
        feedback.mkdir()
        runs.mkdir()
        (feedback / "2026-01-01.md").write_text(
            "1 [y,a] Senior Engineer | Acme ::\n", encoding="utf-8")
        (runs / "ranked_2026-01-01.json").write_text(json.dumps(
            {"ranked": [{"title": "Senior Engineer", "employer": "Acme",
                         "url": "https://example.com/1"}],
             "rejected": []}), encoding="utf-8")
        prompt = self.root / "prompt.txt"
        prompt.write_text("Rank jobs.", encoding="utf-8")
        js.configure({**CONFIG, "prompt_path": str(prompt),
                      "applied_path": str(self.root / "none.txt")})
        rows = [
            {"title": "Senior Software Engineer", "employer": "Acme",
             "url": "https://example.com/1", "source": "raw_boards"},
            {"title": "Data Engineer", "employer": "Acme", "url": "",
             "source": "raw", "location": "Springfield"},
        ]
        with mock.patch.object(js.verdicts, "FEEDBACK_DIR", feedback), \
                mock.patch.object(js.verdicts, "RUNS_DIR", runs), \
                mock.patch.object(js, "APPLIED_URLS", set()), \
                mock.patch.object(js, "APPLIED_PAIRS", set()):
            js.load_inputs()
            kept, rejected = js.hard_filter(rows)
        self.assertEqual([j["title"] for j in rejected],
                         ["Senior Software Engineer"])
        # The digest row has no URL, so it can't match one
        self.assertEqual([j["title"] for j in kept], ["Data Engineer"])

    def test_unreadable_applied_lines_are_reported(self):
        applied = self.root / "applied.txt"
        applied.write_text("Senior Engineer at Acme\n"
                           "Software Engineer #2 | Acme\n"
                           "https://example.com/1\n", encoding="utf-8")
        with mock.patch("builtins.print") as printed:
            urls, pairs = js.load_applied(applied)
        self.assertEqual((urls, pairs), ({"https://example.com/1"}, set()))
        warnings = [c.args[0] for c in printed.call_args_list]
        self.assertEqual(len(warnings), 2)
        self.assertIn(":1:", warnings[0])
        # The " #" cut "#2 | Acme" off as a comment; the warning says so
        self.assertIn(":2:", warnings[1])
        self.assertIn('" #"', warnings[1])

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
        self.assertNotIn("pass1_capped_from", j)

    def test_rerank_keeps_the_raw_pass1_score_of_a_capped_row(self):
        batch = [self.job(employer="Acme Staffing")]
        js.apply_pass1(batch, {0: {"score": 72, "reason": "pass 1"}})
        js.apply_rerank(batch, {0: {"score": 80, "reason": "pass 2"}})
        j = batch[0]
        self.assertEqual((j["pass1_score"], j["pass1_capped_from"],
                          j["score"], j["capped_from"]), (50, 72, 50, 80))

class ModelResponses(unittest.TestCase):
    def test_zero_based_indices_are_kept(self):
        rows = [(0, 70, "a"), (1, 40, "b")]
        self.assertEqual(js.align_indices(rows, 2),
                         {0: {"score": 70, "reason": "a"},
                          1: {"score": 40, "reason": "b"}})

    def test_one_based_indices_are_shifted(self):
        self.assertEqual(sorted(js.align_indices([(1, 70, "a"), (2, 40, "b")],
                                                 2)), [0, 1])

    def test_ambiguous_numbering_is_rejected(self):
        with self.assertRaises(ValueError):
            js.align_indices([(1, 70, "a")], 3)

    def test_out_of_range_indices_are_dropped(self):
        self.assertEqual(list(js.align_indices([(0, 1, ""), (5, 2, "")], 2)),
                         [0])

    def test_truncated_response_is_salvaged(self):
        text = ('{"scores": [{"index": 0, "score": 70, "reason": "ok"}, '
                '{"index": 1, "score": 6')
        with mock.patch("builtins.print"):
            self.assertEqual(js.parse_scores(text, 2, 1),
                             {0: {"score": 70, "reason": "ok"}})

    def test_salvage_never_invents_an_entry(self):
        with self.assertRaises(ValueError):
            js.salvage_scores('{"scores": [{"index": 0, "reason": "a } b')

    def test_unusable_responses(self):
        for text in ("", "   ", "[1, 2]", '{"scores": null}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                js.parse_scores(text, 1, 1)

    def test_scores_are_clamped_and_bad_entries_dropped(self):
        text = ('{"scores": [{"index": 0, "score": 150, "reason": "x"}, '
                '{"index": 1, "score": "high"}, "junk"]}')
        self.assertEqual(js.parse_scores(text, 2, 1),
                         {0: {"score": 100, "reason": "x"}})

    def test_a_failed_retry_keeps_the_earlier_partial_result(self):
        answers = iter([json.dumps({"scores": [
            {"index": 0, "score": 70, "reason": "a"}]}), TimeoutError()])
        def chat(system, user, attempt=1):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(js, "ollama_chat", chat), \
                mock.patch("builtins.print"):
            self.assertEqual(js.request_scores("s", "u", [{}, {}]),
                             {0: {"score": 70, "reason": "a", "attempt": 1}})

    def test_the_answering_attempt_is_recorded(self):
        answers = iter(["not json", json.dumps({"scores": [
            {"index": 0, "score": 55, "reason": "b"}]})])
        with mock.patch.object(js, "ollama_chat",
                               lambda system, user, attempt=1: next(answers)), \
                mock.patch("builtins.print"):
            self.assertEqual(js.request_scores("s", "u", [{}]),
                             {0: {"score": 55, "reason": "b", "attempt": 2}})

    def test_a_failed_first_attempt_fails_the_batch(self):
        with mock.patch.object(js, "ollama_chat", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                js.request_scores("s", "u", [{}])

    def http_error(self, code, reason, body):
        return urllib.error.HTTPError("http://localhost:11434/api/chat",
                                      code, reason, {}, io.BytesIO(body))

    def test_a_4xx_is_rejected_with_ollamas_reason(self):
        error = self.http_error(
            400, "Bad Request",
            b'{"error": "\\"gemma\\" does not support thinking"}')
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(js.RequestRejected) as cm:
            js.ollama_chat("s", "u")
        self.assertEqual(str(cm.exception), 'HTTP 400 Bad Request: "gemma" '
                                            'does not support thinking')

    def test_a_5xx_is_a_transport_error_with_ollamas_reason(self):
        error = self.http_error(500, "Internal Server Error",
                                b'{"error": "model requires more memory"}')
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(js.TRANSPORT_ERRORS) as cm:
            js.ollama_chat("s", "u")
        self.assertNotIsInstance(cm.exception, js.RequestRejected)
        self.assertIn("model requires more memory", str(cm.exception))

    def test_request_text_is_pinned(self):
        # The request text is part of what's scored: changing it changes
        # scores, so a change here should be deliberate
        job = {"title": "Senior Engineer", "employer": "Acme",
               "location": "Springfield",
               "description": "About the role\nBuild things."}
        with mock.patch.object(js, "request_scores",
                               side_effect=lambda system, user, batch: user):
            pass1, pass2 = js.score_batch([job]), js.score_rerank([job])
        reply = ('Respond with STRICT JSON only, format: {"scores": '
                 '[{"index": <int>, "score": <int 0-100>, "reason": '
                 '"<one short sentence>"}]}. Score each job on its own: '
                 'its score must not depend on the other jobs in this '
                 'list. Include every index exactly once.')
        self.assertEqual(pass1, f"Score every job below for fit. {reply}"
                                f"\n\nJobs:\n0: Senior Engineer | Acme | "
                                f"Springfield")
        self.assertEqual(pass2, f"Re-score every job below for fit using "
                                f"the posting text. {reply}\n\nJobs:\n0: "
                                f"Senior Engineer | Acme | Springfield\n"
                                f"POSTING: About the role\nBuild things.")

    def test_model_available(self):
        tags = {"models": [{"name": "gemma4:12b"}, {"name": "other:latest"}]}
        self.assertTrue(js.model_available(tags, "gemma4:12b"))
        self.assertTrue(js.model_available(tags, "other"))
        self.assertFalse(js.model_available(tags, "gemma4"))
        self.assertFalse(js.model_available({}, "gemma4:12b"))

class RoleExcerpt(unittest.TestCase):
    def test_starts_at_a_heading(self):
        desc = ("Example Co builds things. We raised money.\n"
                "About the role\nYou build things.")
        self.assertTrue(js.role_excerpt(desc, 500).startswith(
            "About the role"))

    def test_markers_in_closing_legal_text_are_ignored(self):
        desc = ("Example Co builds things. " * 20 + "\nEqual opportunity "
                "employer; we consider qualifications and merit.")
        self.assertTrue(js.role_excerpt(desc, 2000).startswith("Example Co"))

    def test_a_late_marker_in_running_text_is_ignored(self):
        desc = "Intro text. " * 20 + "We are hiring until the role is filled."
        self.assertTrue(js.role_excerpt(desc, 2000).startswith("Intro text."))

    def test_long_excerpt_is_cut_at_a_sentence_end(self):
        desc = "About the role\n" + "One sentence here. " * 30
        excerpt = js.role_excerpt(desc, 100)
        self.assertLessEqual(len(excerpt), 100)
        self.assertTrue(excerpt.endswith("."))

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

    def test_a_negative_count_is_rejected(self):
        for key in ("shortlist_size", "lookup_size", "shortlist_per_employer",
                    "rerank_top_n", "recency_days", "excerpt_chars",
                    "score_cache_days"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                js.configure({**CONFIG, key: -1})

    def test_a_batch_or_context_of_zero_is_rejected_at_startup(self):
        # 0 used to pass, then stop the run after the Ollama check
        for key in ("pass1_batch", "rerank_batch", "num_ctx"):
            with self.subTest(key=key), \
                    self.assertRaisesRegex(ValueError, "at least 1"):
                js.configure({**CONFIG, key: 0})
        for key in ("rerank_top_n", "excerpt_chars", "recency_days"):
            with self.subTest(key=key):
                js.configure({**CONFIG, key: 0})  # 0 has a meaning here

class Shortlist(unittest.TestCase):
    def tearDown(self):
        js.configure(CONFIG)

    def test_per_employer_limit_ignores_case(self):
        js.configure({**CONFIG, "shortlist_per_employer": 1})
        ranked = [{"title": f"Role {i}", "employer": e}
                  for i, e in enumerate(["Acme", "ACME", "Beta", "acme"])]
        (rows,), more = js.shortlist_rows((ranked, 25))
        self.assertEqual([j["title"] for j in rows], ["Role 0", "Role 2"])
        self.assertEqual(dict(more), {"Acme": 2})
        self.assertEqual([j["title"] for j in ranked
                          if j.get("employer_limited")], ["Role 1", "Role 3"])

    def test_a_section_stops_at_its_size(self):
        ranked = [{"title": f"Role {i}", "employer": f"Co {i}"}
                  for i in range(4)]
        (rows,), more = js.shortlist_rows((ranked, 2))
        self.assertEqual([j["title"] for j in rows], ["Role 0", "Role 1"])
        self.assertEqual(more, {})

    def test_the_per_employer_limit_spans_both_sections(self):
        js.configure({**CONFIG, "shortlist_per_employer": 2})
        main = [{"title": f"Board {i}", "employer": "Acme"} for i in range(3)]
        lookup = [{"title": f"Digest {i}", "employer": "ACME"}
                  for i in range(3)]
        (rows, lookup_rows), more = js.shortlist_rows((main, 15),
                                                      (lookup, 10))
        self.assertEqual([j["title"] for j in rows], ["Board 0", "Board 1"])
        self.assertEqual(lookup_rows, [])
        # One entry under the first spelling, not one per section
        self.assertEqual(dict(more), {"Acme": 4})

    def test_a_judged_no_ranks_below_others_at_its_score(self):
        kept = [{"title": "Ruled out", "score": 50, "flags": ["judged no"]},
                {"title": "Higher", "score": 70},
                {"title": "Capped by a rule", "score": 50, "flags": ["rails"]},
                {"title": "Unjudged", "score": 50}]
        kept.sort(key=js.rank_order, reverse=True)
        self.assertEqual([j["title"] for j in kept],
                         ["Higher", "Capped by a rule", "Unjudged",
                          "Ruled out"])

class VerdictCaps(unittest.TestCase):
    """A listing judged "n" is held at no_verdict_cap after scoring."""
    def tearDown(self):
        js.configure(CONFIG)

    def jobs(self):
        return [{"title": "Senior Engineer", "employer": "Acme", "url": "",
                 "score": 80},
                {"title": "Data Engineer", "employer": "Beta",
                 "url": "https://example.com/1", "score": 40},
                {"title": "Lead Engineer", "employer": "Gamma", "url": "",
                 "score": 90}]

    def capped(self, cfg):
        js.configure(cfg)
        jobs = self.jobs()
        with mock.patch.object(js, "NO_PAIRS", {("senior engineer", "acme")}), \
                mock.patch.object(js, "NO_URLS", {"https://example.com/1"}):
            lowered = js.apply_verdict_caps(jobs)
        return lowered, jobs

    def test_a_no_caps_by_listing_or_url_and_flags_it(self):
        lowered, jobs = self.capped(CONFIG)
        senior, data, lead = jobs
        self.assertEqual(lowered, 1)
        self.assertEqual((senior["score"], senior["verdict_capped_from"],
                          senior["flags"]), (50, 80, ["judged no"]))
        # Already under the cap: flagged, not lowered
        self.assertEqual((data["score"], data["flags"]), (40, ["judged no"]))
        self.assertNotIn("verdict_capped_from", data)
        self.assertNotIn("flags", lead)

    def test_null_turns_the_cap_off(self):
        lowered, jobs = self.capped({**CONFIG, "no_verdict_cap": None})
        self.assertEqual(lowered, 0)
        self.assertEqual([j["score"] for j in jobs], [80, 40, 90])
        self.assertFalse(any("flags" in j for j in jobs))

    def test_only_the_latest_verdict_counts_and_applied_wins(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        feedback = root / "feedback"
        feedback.mkdir()
        (feedback / "2026-01-01.md").write_text(
            "1 [n] Senior Engineer | Acme ::\n"
            "2 [n] Data Engineer | Beta ::\n"
            "3 [n,a] Lead Engineer | Gamma ::\n", encoding="utf-8")
        (feedback / "2026-01-02.md").write_text(
            "1 [m] Data Engineer | Beta ::\n", encoding="utf-8")
        prompt = root / "prompt.txt"
        prompt.write_text("Rank jobs.", encoding="utf-8")
        js.configure({**CONFIG, "prompt_path": str(prompt),
                      "applied_path": str(root / "none.txt")})
        with mock.patch.object(js.verdicts, "FEEDBACK_DIR", feedback), \
                mock.patch.object(js.verdicts, "RUNS_DIR", root), \
                mock.patch.object(js, "APPLIED_URLS", set()), \
                mock.patch.object(js, "APPLIED_PAIRS", set()), \
                mock.patch.object(js, "NO_URLS", set()), \
                mock.patch.object(js, "NO_PAIRS", set()):
            js.load_inputs()
            self.assertEqual(js.NO_PAIRS, {("senior engineer", "acme")})
            self.assertIn(("lead engineer", "gamma"), js.APPLIED_PAIRS)

class VerdictTemplate(unittest.TestCase):
    """write_verdict_template on a new file, and on a same-day rerun."""
    STAMP = "2026-01-02"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "feedback" / f"{self.STAMP}.md"

    def rows(self, *titles):
        return [{"title": t, "employer": "Acme"} for t in titles]

    def write(self, *titles):
        return js.write_verdict_template(self.path, self.STAMP,
                                         self.rows(*titles))

    def test_a_new_file_gets_the_template(self):
        self.assertEqual(self.write("Senior Engineer"), "written")
        self.assertIn("1 [?] Senior Engineer | Acme ::",
                      self.path.read_text(encoding="utf-8"))

    def test_an_untouched_template_follows_the_rerun(self):
        self.write("Senior Engineer", "Data Engineer")
        self.assertEqual(self.write("Senior Engineer", "Data Engineer"),
                         "unchanged")
        self.assertTrue(self.write("Lead Engineer").startswith("rewritten"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("1 [?] Lead Engineer | Acme ::", text)
        self.assertNotIn("Data Engineer", text)

    def test_a_filled_file_only_gets_new_rows_appended(self):
        for eol in ("\r\n", "\n"):
            with self.subTest(line_endings=repr(eol)):
                self.path.unlink(missing_ok=True)
                self.write("Senior Engineer", "Data Engineer")
                text = self.path.read_text(encoding="utf-8").replace(
                    "1 [?]", "1 [y]").replace("\n", eol) + "my notes"
                self.path.write_bytes(text.encode("utf-8"))
                self.assertEqual(self.write("Data Engineer", "Lead Engineer",
                                            "Senior Engineer"),
                                 "1 row(s) new to the list appended")
                after = self.path.read_bytes().decode("utf-8")
                # Everything that was there stays byte for byte; the
                # last line is ended, then a blank line and the new rows
                self.assertTrue(after.startswith(text + eol))
                self.assertEqual(after[len(text):].split(eol),
                                 ["", "", after.split(eol)[-3],
                                  "2 [?] Lead Engineer | Acme ::", ""])
                self.assertTrue(after.split(eol)[-3].startswith(
                    "# Rerun at "))
                self.assertEqual(self.write("Lead Engineer"), "unchanged")

class FakeModel:
    """Stands in for ollama_chat: scores every listing in a request 60,
    and raises error instead for the requests when(user) picks."""
    def __init__(self, error=None, when=lambda user: True):
        self.error, self.when, self.requests = error, when, []

    def __call__(self, system, user, attempt=1):
        self.requests.append(user)
        if self.error and self.when(user):
            raise self.error
        n = len(re.findall(r"^\d+: ", user, re.M))
        return json.dumps({"scores": [{"index": i, "score": 60,
                                       "reason": "fits"} for i in range(n)]})

def pass1(user):
    return user.startswith("Score")

class MainRunBase(SourceFiles):
    """main() end to end in a temporary folder, with FakeModel for
    Ollama."""
    def setUp(self):
        super().setUp()
        prompt = self.root / "prompt.txt"
        prompt.write_text("Rank jobs.", encoding="utf-8")
        config = self.root / "pipeline_config.json"
        # 4 listings: 2 pass-1 batches. The 2 board rows have a
        # description: 2 pass-2 batches
        config.write_text(json.dumps({
            **CONFIG, "prompt_path": str(prompt),
            "applied_path": str(self.root / "applied.txt"),
            "pass1_batch": 2, "rerank_top_n": 2, "rerank_batch": 1}),
            encoding="utf-8")
        self.out, self.feedback = self.root / "out", self.root / "feedback"
        for target, name, value in (
                (pipeline_common, "CONFIG_FILE", config),
                (js, "OUT_DIR", self.out), (js, "TEMPLATE_DIR", None),
                (js.verdicts, "FEEDBACK_DIR", self.feedback),
                (js.verdicts, "RUNS_DIR", self.out)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(js.configure, CONFIG)
        self.write(self.raw, "job_uid_1.txt", digest_text(
            1, ("Data Engineer", "Beta", "Springfield"),
            ("Platform Engineer", "Gamma", "Springfield")))
        for n, (title, employer) in enumerate(
                [("Senior Engineer", "Acme"), ("Backend Engineer", "Delta")],
                1):
            self.write(self.boards, f"greenhouse_{n}.txt", board_text(
                title, employer, "Springfield", f"https://example.com/{n}",
                "About the role\nBuild things.", posted=f"2026-09-0{n}"))
        self.stamp = date.today().isoformat()

    def set_config(self, **settings):
        config = json.loads(pipeline_common.CONFIG_FILE.read_text(
            encoding="utf-8"))
        pipeline_common.CONFIG_FILE.write_text(json.dumps(
            {**config, **settings}), encoding="utf-8")

    def run_main(self, model, preflight=True, argv=()):
        """main()'s exit code and what it printed; preflight=False runs
        the real Ollama check."""
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(js, "ollama_chat", model))
            if preflight:
                stack.enter_context(mock.patch.object(js, "ollama_preflight"))
            stack.enter_context(contextlib.redirect_stdout(out))
            try:
                js.main(argv)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def written(self):
        return sorted(p.relative_to(self.root).as_posix()
                      for d in (self.out, self.feedback) if d.exists()
                      for p in d.iterdir())

    def shortlist(self):
        return (self.out / f"shortlist_{self.stamp}.md").read_text(
            encoding="utf-8")

    def ranked(self):
        return json.loads((self.out / f"ranked_{self.stamp}.json")
                          .read_text(encoding="utf-8"))

class MainRun(MainRunBase):
    """Batch scoring. Pins the exit codes run_screen.bat relies on: 2
    means nothing was written, so a retry is safe; 1 a partial run, or a
    problem a retry can't fix; 0 success."""
    def test_everything_scores(self):
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(self.written(), [f"feedback/{self.stamp}.md",
                                          f"out/ranked_{self.stamp}.json",
                                          f"out/shortlist_{self.stamp}.md"])
        data = self.ranked()
        self.assertEqual((data["failed_batches"],
                          data["rerank_failed_batches"]), (0, 0))
        self.assertEqual(sum("rescored" in j for j in data["ranked"]), 2)
        self.assertEqual(len(model.requests), 4)
        self.assertNotIn("WARNING", self.shortlist())

    def test_the_json_records_its_run_and_not_the_posting_text(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        data = self.ranked()
        run = data["run"]
        self.assertEqual((run["model"], run["prompt_sha"], run["pass1_batch"]),
                         (js.MODEL, js.fingerprint("Rank jobs."), 2))
        self.assertEqual(run["score_caps_sha"], js.fingerprint(
            json.dumps(CONFIG["score_caps"], sort_keys=True)))
        rows = data["ranked"] + data["rejected"]
        self.assertFalse(any("description" in j for j in rows))
        self.assertEqual({j["excerpt"] for j in rows if j.get("rescored")},
                         {"About the role\nBuild things."})

    def test_the_shortlist_shows_when_board_rows_were_posted(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        rows = {ln.split(" | ")[3]: ln.split(" | ")[6]
                for ln in self.shortlist().splitlines()
                if ln.startswith("| ") and ln[2].isdigit()}
        self.assertEqual(rows, {"Senior Engineer": "2026-09-01",
                                "Backend Engineer": "2026-09-02",
                                "Data Engineer": "-",
                                "Platform Engineer": "-"})
        ranked = {j["title"]: j for j in self.ranked()["ranked"]}
        self.assertEqual(ranked["Senior Engineer"]["posted"], "2026-09-01")
        self.assertNotIn("posted", ranked["Data Engineer"])

    def test_ollama_unreachable_writes_nothing(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            code, _ = self.run_main(FakeModel(), preflight=False)
        self.assertEqual(code, 2)
        self.assertEqual(self.written(), [])

    def test_every_pass1_batch_failing_writes_nothing(self):
        code, _ = self.run_main(FakeModel(ConnectionResetError()))
        self.assertEqual(code, 2)
        self.assertEqual(self.written(), [])

    def test_a_rejected_request_writes_nothing_and_stops_at_once(self):
        model = FakeModel(js.RequestRejected("HTTP 400 Bad Request: "
                                             "no thinking"))
        code, printed = self.run_main(model)
        # 1, not 2: the wrapper's retry would send the same request
        self.assertEqual(code, 1)
        self.assertEqual(self.written(), [])
        self.assertEqual(len(model.requests), 1)
        self.assertIn("no thinking", printed)

    def test_one_failed_pass1_batch_is_a_partial_run(self):
        model = FakeModel(TimeoutError(),
                          when=lambda u: pass1(u) and "Data Engineer" in u)
        self.assertEqual(self.run_main(model)[0], 1)
        self.assertIn("**WARNING: 1 of 2 pass-1 batches failed",
                      self.shortlist())
        self.assertEqual(self.ranked()["failed_batches"], 1)

    def test_a_failed_pass2_is_a_partial_run(self):
        model = FakeModel(TimeoutError(), when=lambda u: not pass1(u))
        code, printed = self.run_main(model)
        self.assertEqual(code, 1)
        self.assertIn("**NOTE: 2 re-scoring batch(es) failed",
                      self.shortlist())
        self.assertEqual(self.ranked()["rerank_failed_batches"], 2)
        self.assertIn("[ERROR] 2 re-scoring batch(es) failed", printed)
        # Nothing was read against a posting, so every row is title-only
        main, lookup = self.shortlist().split("## Needs lookup")
        self.assertIn("No listings.", main)
        self.assertIn("| Senior Engineer |", lookup)

    def test_a_board_row_pass2_skipped_goes_under_needs_lookup(self):
        config = json.loads(pipeline_common.CONFIG_FILE.read_text(
            encoding="utf-8"))
        pipeline_common.CONFIG_FILE.write_text(json.dumps(
            {**config, "rerank_top_n": 1}), encoding="utf-8")
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        main, lookup = self.shortlist().split("## Needs lookup")
        rescored = [j["title"] for j in self.ranked()["ranked"]
                    if j.get("rescored")]
        self.assertEqual(len(rescored), 1)
        self.assertIn(f"| {rescored[0]} |", main)
        other = ({"Senior Engineer", "Backend Engineer"} - set(rescored)).pop()
        self.assertIn(f"| {other} |", lookup)

    def test_rows_without_posting_text_have_their_own_section(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        text = self.shortlist()
        main, lookup = text.split("## Needs lookup")
        def numbered(section):
            return [(ln.split(" | ")[0], ln.split(" | ")[3])
                    for ln in section.splitlines()
                    if ln.startswith("| ") and ln[2].isdigit()]
        # Every score is 60, so only the sections decide the split.
        # Numbering runs on through both, as in the template
        self.assertEqual({t for _, t in numbered(main)},
                         {"Senior Engineer", "Backend Engineer"})
        self.assertEqual([n for n, _ in numbered(main)], ["| 1", "| 2"])
        self.assertEqual({t for _, t in numbered(lookup)},
                         {"Data Engineer", "Platform Engineer"})
        self.assertEqual([n for n, _ in numbered(lookup)], ["| 3", "| 4"])
        ranks = {j["title"]: j["shortlist_rank"]
                 for j in self.ranked()["ranked"]}
        template = (self.feedback / f"{self.stamp}.md").read_text(
            encoding="utf-8")
        for title, n in ranks.items():
            self.assertIn(f"{n} [?] {title} |", template)

    def test_a_section_size_comes_from_config(self):
        config = json.loads(pipeline_common.CONFIG_FILE.read_text(
            encoding="utf-8"))
        pipeline_common.CONFIG_FILE.write_text(json.dumps(
            {**config, "shortlist_size": 1, "lookup_size": 0}),
            encoding="utf-8")
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        ranked = self.ranked()
        self.assertEqual(sum("shortlist_rank" in j for j in ranked["ranked"]),
                         1)
        self.assertEqual((ranked["run"]["shortlist_size"],
                          ranked["run"]["lookup_size"]), (1, 0))
        self.assertIn("## Needs lookup", self.shortlist())
        self.assertIn("No listings.", self.shortlist())

    def test_a_no_verdict_caps_the_listing_after_both_passes(self):
        self.feedback.mkdir()
        (self.feedback / "2026-01-01.md").write_text(
            "1 [n] Senior Engineer | Acme ::\n"
            "2 [n] Data Engineer | Beta ::\n", encoding="utf-8")
        model = FakeModel()
        code, printed = self.run_main(model)
        self.assertEqual(code, 0)
        # Pass 2 still re-scored both board rows: the cap comes after it
        self.assertEqual(len(model.requests), 4)
        ranked = {j["title"]: j for j in self.ranked()["ranked"]}
        for title in ("Senior Engineer", "Data Engineer"):
            self.assertEqual((ranked[title]["score"],
                              ranked[title]["verdict_capped_from"],
                              ranked[title]["flags"]),
                             (50, 60, ["judged no"]))
        self.assertEqual(ranked["Backend Engineer"]["score"], 60)
        self.assertEqual(ranked["Backend Engineer"]["shortlist_rank"], 1)
        self.assertEqual(ranked["Senior Engineer"]["shortlist_rank"], 2)
        self.assertEqual(self.ranked()["run"]["no_verdict_cap"], 50)
        self.assertIn("| 2 | ▲ 50 | judged no, capped | Senior Engineer |",
                      self.shortlist())
        self.assertIn("\"No\" verdicts held 2 score(s) at 50.", printed)
        # Not asked about again; the rest keep their shortlist numbers
        template = (self.feedback / f"{self.stamp}.md").read_text(
            encoding="utf-8")
        self.assertNotIn("Senior Engineer", template)
        self.assertNotIn("Data Engineer", template)
        self.assertIn("1 [?] Backend Engineer | Delta ::", template)
        n = ranked["Platform Engineer"]["shortlist_rank"]
        self.assertIn(f"{n} [?] Platform Engineer | Gamma ::", template)

    def test_a_digest_without_listings_is_flagged_in_the_shortlist(self):
        self.write(self.raw, "job_uid_2.txt", digest_text(1) + "New layout\n")
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        self.assertIn("**WARNING: 1 recent digest file(s) have text but no "
                      "listings (job_uid_2.txt)", self.shortlist())

    def test_an_unknown_argument_stops_the_run(self):
        code, printed = self.run_main(FakeModel(), argv=["--rescor"])
        # 1, not 2: a retry would pass the same argument
        self.assertEqual(code, 1)
        self.assertIn("usage:", printed)
        self.assertEqual(self.written(), [])

    def test_rescore_does_nothing_in_batch_mode(self):
        code, printed = self.run_main(FakeModel(), argv=["--rescore"])
        self.assertEqual(code, 0)
        self.assertIn("--rescore ignored", printed)
        self.assertNotIn("out/score_cache.json", self.written())

class ScoreByText(FakeModel):
    """FakeModel whose scores differ by request, so that an unchanged
    score means the same request, or the cache."""
    def __call__(self, system, user, attempt=1):
        self.requests.append(user)
        if self.error and self.when(user):
            raise self.error
        lines = re.findall(r"^(\d+): (.*)$", user, re.M)
        whole = user if len(lines) == 1 else ""
        scores = [int(hashlib.sha256((system + line + whole).encode())
                      .hexdigest(), 16) % 101 for _, line in lines]
        return json.dumps({"scores": [
            {"index": int(i), "score": s, "reason": f"r{s}"}
            for (i, _), s in zip(lines, scores)]})

class PerListingRerank(MainRunBase):
    """per_listing_rerank: pass 1 in batches as before, then pass 2 over
    every board row with posting text, one per request, through the
    score cache. The exit codes keep their meaning; a pass-2 failure
    count is of requests."""
    def setUp(self):
        super().setUp()
        # rerank_top_n is unused: every board row is re-scored
        self.set_config(per_listing_rerank=True, rerank_top_n=1)

    def scores(self):
        return {j["title"]: (j["score"], j.get("capped_from"), j["reason"])
                for j in self.ranked()["ranked"] if j.get("rescored")}

    def rescored(self, model):
        """The titles pass 2 sent, in order."""
        return sorted(re.search(r"^0: (.*?) \|", u, re.M).group(1)
                      for u in model.requests if not pass1(u))

    def test_every_posting_is_rescored_in_its_own_request(self):
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        # Pass 1: 2 batches of 2, as in batch scoring
        self.assertEqual(sum(map(pass1, model.requests)), 2)
        self.assertEqual(self.rescored(model),
                         ["Backend Engineer", "Senior Engineer"])
        for user in model.requests:
            if not pass1(user):
                self.assertEqual(len(re.findall(r"^\d+: ", user, re.M)), 1)
        data = self.ranked()
        self.assertEqual(sum("rescored" in j for j in data["ranked"]), 2)
        self.assertTrue(data["run"]["per_listing_rerank"])
        self.assertEqual(data["score_cache"],
                         {"cached": 0, "scored": 2, "failed": 0,
                          "entries": 2, "dropped": 0, "rescore": False})
        self.assertIn("out/score_cache.json", self.written())
        self.assertNotIn("WARNING", self.shortlist())

    def test_a_request_is_the_one_batch_scoring_sends_for_one_listing(self):
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        job = {"title": "Senior Engineer", "employer": "Acme",
               "location": "Springfield",
               "description": "About the role\nBuild things."}
        self.assertIn(js.rerank_request([job])[1], model.requests)

    def test_a_second_run_rescores_nothing_and_keeps_the_scores(self):
        self.assertEqual(self.run_main(ScoreByText())[0], 0)
        first = self.scores()
        model = ScoreByText()
        code, printed = self.run_main(model)
        self.assertEqual(code, 0)
        self.assertEqual(self.rescored(model), [])
        self.assertEqual(self.scores(), first)
        self.assertEqual(self.ranked()["score_cache"]["cached"], 2)
        self.assertIn("2 of 2 listings with posting text from the score "
                      "cache", printed)

    def test_an_edited_posting_alone_is_rescored(self):
        self.assertEqual(self.run_main(ScoreByText())[0], 0)
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "Senior Engineer", "Acme", "Springfield", "https://example.com/1",
            "About the role\nBuild other things.", posted="2026-09-01"))
        model = ScoreByText()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(self.rescored(model), ["Senior Engineer"])
        # And a new location, or a new title
        self.write(self.boards, "greenhouse_2.txt", board_text(
            "Backend Engineer", "Delta", "Remote", "https://example.com/2",
            "About the role\nBuild things.", posted="2026-09-02"))
        self.write(self.boards, "greenhouse_1.txt", board_text(
            "Senior Engineer II", "Acme", "Springfield",
            "https://example.com/1", "About the role\nBuild other things.",
            posted="2026-09-01"))
        model = ScoreByText()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(self.rescored(model),
                         ["Backend Engineer", "Senior Engineer II"])

    def test_a_prompt_change_rescores_everything(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        (self.root / "prompt.txt").write_text("Rank jobs strictly.",
                                              encoding="utf-8")
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(len(self.rescored(model)), 2)

    def test_a_cap_change_applies_without_a_rescore(self):
        self.set_config(score_caps=[{"employers": ["Acme"], "max_score": 50}])
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        self.assertEqual(self.scores()["Senior Engineer"][:2], (50, 60))
        self.set_config(score_caps=[{"employers": ["Acme"], "max_score": 30}])
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(self.rescored(model), [])
        # The cache holds the model's own score, 60, not the old cap's 50
        self.assertEqual(self.scores()["Senior Engineer"][:2], (30, 60))

    def test_rescore_sends_every_request_again(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        model = FakeModel()
        self.assertEqual(self.run_main(model, argv=["--rescore"])[0], 0)
        self.assertEqual(len(self.rescored(model)), 2)
        self.assertTrue(self.ranked()["score_cache"]["rescore"])

    def test_ollama_unreachable_writes_nothing(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("refused")):
            code, _ = self.run_main(FakeModel(), preflight=False)
        self.assertEqual(code, 2)
        self.assertEqual(self.written(), [])

    def test_every_pass1_batch_failing_writes_nothing(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        for p in self.out.iterdir():
            if p.name != "score_cache.json":
                p.unlink()
        before = (self.out / "score_cache.json").read_bytes()
        code, _ = self.run_main(FakeModel(ConnectionResetError()))
        self.assertEqual(code, 2)
        # Not even the cache: the exit-2 path writes nothing
        self.assertEqual([p.name for p in self.out.iterdir()],
                         ["score_cache.json"])
        self.assertEqual((self.out / "score_cache.json").read_bytes(), before)

    def test_a_failed_request_is_a_partial_run_and_isnt_cached(self):
        model = FakeModel(TimeoutError(),
                          when=lambda u: not pass1(u) and "Acme" in u)
        code, printed = self.run_main(model)
        self.assertEqual(code, 1)
        self.assertIn("**NOTE: 1 re-scoring request(s) failed",
                      self.shortlist())
        self.assertIn("[ERROR] 1 re-scoring request(s) failed", printed)
        main, lookup = self.shortlist().split("## Needs lookup")
        self.assertIn("| Senior Engineer |", lookup)
        model = FakeModel()
        self.assertEqual(self.run_main(model)[0], 0)
        self.assertEqual(self.rescored(model), ["Senior Engineer"])

    def test_cached_scores_apply_when_pass1_aborted(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        jobs = [{"title": t, "employer": e, "location": "Springfield",
                 "description": d, "score": 40, "reason": "pass 1"}
                for t, e, d in (("Senior Engineer", "Acme",
                                 "About the role\nBuild things."),
                                ("Senior Engineer", "Zeta",
                                 "About the role\nNew."))]
        model = FakeModel()
        with mock.patch.object(js, "ollama_chat", model), \
                mock.patch("builtins.print"):
            failed, counts = js.rerank_per_listing(jobs, aborted="failures")
        self.assertEqual(model.requests, [])
        self.assertEqual((failed, counts["cached"]), (1, 1))
        self.assertEqual((jobs[0]["score"], jobs[0].get("rescored")),
                         (60, True))
        self.assertEqual((jobs[1]["score"], jobs[1].get("rescored")),
                         (40, None))

    def test_a_retrys_score_is_cached_with_its_attempt(self):
        fake = FakeModel()
        def model(system, user, attempt=1):
            if not pass1(user) and "Acme" in user and attempt == 1:
                return "not json"
            return fake(system, user, attempt)
        with mock.patch("builtins.print"):
            self.assertEqual(self.run_main(model)[0], 0)
        entries = json.loads((self.out / "score_cache.json").read_text(
            encoding="utf-8"))["entries"].values()
        self.assertEqual(sorted((e["employer"], e["attempt"]) for e in entries),
                         [("Acme", 2), ("Delta", 1)])

    def test_entries_unused_for_score_cache_days_are_dropped(self):
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        path = self.out / "score_cache.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["entries"]["stale"] = {"score": 1, "reason": "",
                                    "scored": "2020-01-01",
                                    "used": "2020-01-01"}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        self.assertNotIn("stale", json.loads(
            path.read_text(encoding="utf-8"))["entries"])
        self.assertEqual(self.ranked()["score_cache"]["dropped"], 1)

class ContextUse(unittest.TestCase):
    def setUp(self):
        js.configure(CONFIG)

    def test_a_request_records_its_prompt_and_response_tokens(self):
        body = json.dumps({"message": {"content": "{}"},
                           "prompt_eval_count": 2118, "eval_count": 46})
        with mock.patch.object(js, "CONTEXT_USED", 0), \
                mock.patch("urllib.request.urlopen",
                           return_value=io.BytesIO(body.encode())):
            js.ollama_chat("s", "u")
            self.assertEqual(js.CONTEXT_USED, 2164)

    def test_a_response_without_counts_records_nothing(self):
        body = json.dumps({"message": {"content": "{}"}})
        with mock.patch.object(js, "CONTEXT_USED", 0), \
                mock.patch("urllib.request.urlopen",
                           return_value=io.BytesIO(body.encode())):
            js.ollama_chat("s", "u")
            self.assertEqual(js.CONTEXT_USED, 0)

    def run_pass(self, used):
        def scorer(batch):
            js.CONTEXT_USED = max(js.CONTEXT_USED, batch[0]["used"])
            return {0: {"score": 1, "reason": ""}}
        out = io.StringIO()
        with mock.patch.dict(js.CONTEXT_PEAKS, clear=True), \
                contextlib.redirect_stdout(out):
            js.run_batches("batch", [[{"used": n}] for n in used], scorer,
                           lambda batch, scores: None)
            peaks = dict(js.CONTEXT_PEAKS)
        return peaks, out.getvalue()

    def test_each_pass_logs_its_peak(self):
        peaks, printed = self.run_pass([3000, 3676, 2500])
        self.assertEqual(peaks, {"batch": 3676})
        self.assertIn("Peak context per batch: 3676 of 8192 tokens\n",
                      printed)

    def test_a_peak_at_num_ctx_is_a_warning(self):
        _, printed = self.run_pass([8192])
        self.assertIn("[WARN] at the limit", printed)

    def test_successes_can_be_logged_every_nth(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            js.run_batches("listing", [[{}]] * 5,
                           lambda batch: {0: {"score": 1, "reason": ""}},
                           lambda batch, scores: None, report_every=2)
        self.assertEqual(re.findall(r"listing (\d+):", out.getvalue()),
                         ["2", "4", "5"])

if __name__ == "__main__":
    unittest.main()
