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
        jobs, _ = js.parse_jobs()
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
                             {0: {"score": 70, "reason": "a"}})

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

class MainRun(SourceFiles):
    """main() end to end in a temporary folder, with FakeModel for
    Ollama. Pins the exit codes run_screen.bat relies on: 2 means
    nothing was written, so a retry is safe; 1 a partial run, or a
    problem a retry can't fix; 0 success."""
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
                "About the role\nBuild things."))
        self.stamp = date.today().isoformat()

    def run_main(self, model, preflight=True):
        """main()'s exit code and what it printed; preflight=False runs
        the real Ollama check."""
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(js, "ollama_chat", model))
            if preflight:
                stack.enter_context(mock.patch.object(js, "ollama_preflight"))
            stack.enter_context(contextlib.redirect_stdout(out))
            try:
                js.main()
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

    def test_a_digest_without_listings_is_flagged_in_the_shortlist(self):
        self.write(self.raw, "job_uid_2.txt", digest_text(1) + "New layout\n")
        self.assertEqual(self.run_main(FakeModel())[0], 0)
        self.assertIn("**WARNING: 1 recent digest file(s) have text but no "
                      "listings (job_uid_2.txt)", self.shortlist())

if __name__ == "__main__":
    unittest.main()
