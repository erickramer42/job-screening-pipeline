"""Tests for verdicts.py: the listing key, the "Title | Employer" split,
the template round trip, merging verdicts and matching them by posting
URL, the run report, and ordering agreement. Run from the repo root
with: python -m unittest
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verdicts

class SplitListing(unittest.TestCase):
    def test_listing_key_ignores_case_and_outer_spaces(self):
        self.assertEqual(verdicts.listing_key(" Senior Engineer ", "ACME"),
                         ("senior engineer", "acme"))

    def test_splits_at_the_last_pipe(self):
        cases = {
            "Senior Engineer | Acme": ("Senior Engineer", "Acme"),
            "Lead | Platform | Acme": ("Lead | Platform", "Acme"),
            "Senior Engineer|Acme": ("Senior Engineer", "Acme"),
            r"Senior Engineer | Acme \| Partners":
                ("Senior Engineer", "Acme | Partners"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(verdicts.split_listing(text), expected)

    def test_no_listing_without_both_sides(self):
        for text in ("Senior Engineer", "| Acme", "Senior Engineer | ", ""):
            with self.subTest(text=text):
                self.assertIsNone(verdicts.split_listing(text))

class FeedbackFiles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        # The runs the files judge; none unless a test writes one
        self.runs = self.folder / "out"
        self.runs.mkdir()
        patcher = mock.patch.object(verdicts, "RUNS_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_run(self, stem, *rows):
        """out/ranked_<stem>.json with (title, employer, url) rows."""
        (self.runs / f"ranked_{stem}.json").write_text(json.dumps(
            {"ranked": [{"title": t, "employer": e, "url": u}
                        for t, e, u in rows], "rejected": []}),
            encoding="utf-8")

    def test_template_round_trip_with_pipes(self):
        rows = [{"title": "Lead | Platform", "employer": "Acme"},
                {"title": "Senior Engineer", "employer": "Acme | Partners"},
                {"title": "Data Engineer", "employer": "Beta"}]
        lines = verdicts.template_lines("2026-01-02", rows)
        filled = [ln.replace("[?]", "[y]", 1) if ln[:1].isdigit() else ln
                  for ln in lines]
        (self.folder / "2026-01-02.md").write_text(
            "\n".join(filled) + "\n", encoding="utf-8")
        loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual(set(loaded), {verdicts.listing_key(r["title"],
                                                            r["employer"])
                                       for r in rows})

    def test_template_numbers_rows_by_their_shortlist_rank(self):
        rows = [{"title": "Senior Engineer", "employer": "Acme",
                 "shortlist_rank": 1},
                {"title": "Data Engineer", "employer": "Beta",
                 "shortlist_rank": 3}]
        lines = verdicts.template_lines("2026-01-02", rows)
        self.assertEqual(lines[-2:], ["1 [?] Senior Engineer | Acme ::",
                                      "3 [?] Data Engineer | Beta ::"])

    def test_later_file_wins_and_unfilled_rows_are_skipped(self):
        (self.folder / "2026-01-01.md").write_text(
            "Free notes | not a verdict line\n"
            "1 [n] Senior Engineer | Acme :: first look\n"
            "2 [?] Data Engineer | Beta ::\n", encoding="utf-8")
        (self.folder / "2026-01-02.md").write_text(
            "- [y] Senior Engineer | ACME :: second look\n", encoding="utf-8")
        loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual(list(loaded), [("senior engineer", "acme")])
        self.assertEqual(loaded[("senior engineer", "acme")]["verdict"], "y")
        self.assertEqual(loaded[("senior engineer", "acme")]["note"],
                         "second look")

    def test_a_later_line_updates_the_verdict_rather_than_replacing_it(self):
        (self.folder / "2026-01-01.md").write_text(
            "1 [y] Senior Engineer | Acme :: strong fit\n"
            "2 [m,a] Data Engineer | Beta :: applied\n", encoding="utf-8")
        (self.folder / "2026-01-02.md").write_text(
            "1 [a] Senior Engineer | Acme ::\n"
            "2 [n] Data Engineer | Beta ::\n", encoding="utf-8")
        loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual({k[1]: (v["verdict"], v["applied"], v["note"])
                          for k, v in loaded.items()},
                         {"acme": ("y", True, "strong fit"),
                          "beta": ("n", True, "applied")})

    def test_a_board_row_keeps_its_verdict_when_its_title_changes(self):
        self.write_run("2026-01-01",
                       ("Senior Engineer", "Acme", "https://example.com/1"),
                       ("Data Engineer", "Beta", ""))
        self.write_run("2026-01-02", ("Senior Software Engineer", "Acme",
                                      "https://example.com/1"))
        (self.folder / "2026-01-01.md").write_text(
            "1 [y] Senior Engineer | Acme :: strong fit\n"
            "2 [m] Data Engineer | Beta ::\n", encoding="utf-8")
        (self.folder / "2026-01-02.md").write_text(
            "1 [a] Senior Software Engineer | Acme ::\n", encoding="utf-8")
        loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual(set(loaded), {("senior software engineer", "acme"),
                                       ("data engineer", "beta")})
        v = loaded[("senior software engineer", "acme")]
        self.assertEqual((v["verdict"], v["applied"], v["note"], v["url"]),
                         ("y", True, "strong fit", "https://example.com/1"))
        # A digest row has no URL, and is matched by title and employer
        self.assertEqual(loaded[("data engineer", "beta")]["url"], "")

    def test_combined_flags(self):
        (self.folder / "2026-01-01.md").write_text(
            "1 [m,a] Senior Engineer | Acme :: maybe, applied anyway\n"
            "2 [a] Data Engineer | Beta ::\n"
            "3 [ Y , A ] Lead Engineer | Gamma ::\n"
            "4 [n] Staff Engineer | Delta ::\n", encoding="utf-8")
        loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual({k[1]: (v["verdict"], v["applied"])
                          for k, v in loaded.items()},
                         {"acme": ("m", True), "beta": (None, True),
                          "gamma": ("y", True), "delta": ("n", False)})

    def test_unreadable_flags_are_reported_not_dropped_silently(self):
        (self.folder / "2026-01-01.md").write_text(
            "1 [y,n] Senior Engineer | Acme ::\n"
            "2 [x] Data Engineer | Beta ::\n"
            "3 [m] Lead Engineer | Gamma ::\n", encoding="utf-8")
        with mock.patch("builtins.print") as printed:
            loaded = verdicts.load_verdicts(self.folder)
        self.assertEqual(list(loaded), [("lead engineer", "gamma")])
        warnings = [c.args[0] for c in printed.call_args_list]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all(":1:" in w or ":2:" in w for w in warnings))

    def test_parse_flags(self):
        self.assertEqual(verdicts.parse_flags("m,a"), ("m", True))
        self.assertEqual(verdicts.parse_flags("a"), (None, True))
        self.assertIsNone(verdicts.parse_flags("?"))
        self.assertIsNone(verdicts.parse_flags(" "))
        for bad in ("y,n", "x", "m,b"):
            with self.subTest(flags=bad), self.assertRaises(ValueError):
                verdicts.parse_flags(bad)

class Evaluate(unittest.TestCase):
    def test_listings_cut_as_applied_are_left_out(self):
        run = {"ranked": [{"title": "Data Engineer", "employer": "Beta",
                           "score": 50, "shortlist_rank": 1}],
               "rejected": [{"title": "Senior Engineer", "employer": "Acme",
                             "reject_reason": "applied"}]}
        v = {("senior engineer", "acme"): {"verdict": "y", "applied": True,
                                           "note": "", "file": "f"},
             ("data engineer", "beta"): {"verdict": "n", "applied": False,
                                         "note": "", "file": "f"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ranked.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                verdicts.evaluate(path, v)
        # Only the ranked "n" listing counts, so there's no pair to order
        self.assertIn("1 on listings in this run (1 marked applied)",
                      out.getvalue())
        self.assertNotIn("ordering agreement", out.getvalue())

    def test_report_matches_by_url_and_names_shortlist_positions(self):
        run = {"ranked": [
            {"title": "Lead Engineer", "employer": "Acme", "score": 90,
             "url": "https://example.com/1", "shortlist_rank": 1},
            {"title": "Senior Engineer", "employer": "Acme", "score": 85,
             "url": "https://example.com/2"},
            {"title": "Data Engineer", "employer": "Beta", "score": 80,
             "url": "", "shortlist_rank": 2},
            {"title": "Platform Engineer", "employer": "Gamma", "score": 20,
             "url": ""}],
            "rejected": []}
        def verdict(grade, url=""):
            return {"verdict": grade, "applied": False, "note": "",
                    "file": "f", "url": url}
        v = {("senior engineer", "acme"):
                 verdict("y", "https://example.com/2"),
             ("data engineer", "beta"): verdict("n"),
             ("platform engineer", "gamma"): verdict("y"),
             # The posting's title has changed since this verdict
             ("engineer", "acme"): verdict("m", "https://example.com/1")}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ranked.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                verdicts.evaluate(path, v)
        report = out.getvalue()
        self.assertIn("4 on listings in this run", report)
        self.assertIn("m: 1 of 1 in the shortlist", report)
        self.assertIn("senior engineer | acme: rank 2, score 85, held out by "
                      "the per-employer limit", report)
        self.assertIn("platform engineer | gamma: rank 4, score 20 [", report)
        self.assertIn("data engineer | beta: shortlist #2, score 80", report)

    def test_a_sectioned_run_names_the_limit_only_where_it_applied(self):
        # A full main section left out "Senior"; the limit left out
        # "Lead". Both rank above the last Needs-lookup row
        run = {"run": {"shortlist_size": 1, "lookup_size": 1},
               "ranked": [
            {"title": "Staff Engineer", "employer": "Acme", "score": 90,
             "shortlist_rank": 1},
            {"title": "Lead Engineer", "employer": "Acme", "score": 85,
             "employer_limited": True},
            {"title": "Senior Engineer", "employer": "Beta", "score": 80},
            {"title": "Data Engineer", "employer": "Gamma", "score": 70,
             "shortlist_rank": 2}],
            "rejected": []}
        v = {k: {"verdict": "y", "applied": False, "note": "", "file": "f"}
             for k in (("lead engineer", "acme"), ("senior engineer", "beta"))}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ranked.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                verdicts.evaluate(path, v)
        report = out.getvalue()
        self.assertIn("lead engineer | acme: rank 2, score 85, held out by "
                      "the per-employer limit", report)
        self.assertIn("senior engineer | beta: rank 3, score 80 [", report)

    def test_a_held_down_row_shows_the_models_score(self):
        run = {"ranked": [
            {"title": "Senior Engineer", "employer": "Acme", "score": 50,
             "verdict_capped_from": 90, "shortlist_rank": 1}],
            "rejected": []}
        v = {("senior engineer", "acme"): {"verdict": "n", "applied": False,
                                           "note": "", "file": "f"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ranked.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                verdicts.evaluate(path, v)
        self.assertIn("senior engineer | acme: shortlist #1, score 90, "
                      "held at 50", out.getvalue())

    def test_ordering_ignores_the_cap_a_no_verdict_put_on(self):
        # The model put the "no" first; its verdict then held it at 50
        run = {"ranked": [
            {"title": "Data Engineer", "employer": "Beta", "score": 70},
            {"title": "Senior Engineer", "employer": "Acme", "score": 50,
             "verdict_capped_from": 90}],
            "rejected": []}
        v = {("data engineer", "beta"): {"verdict": "y", "applied": False,
                                         "note": "", "file": "f"},
             ("senior engineer", "acme"): {"verdict": "n", "applied": False,
                                           "note": "", "file": "f"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ranked.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                verdicts.evaluate(path, v)
        self.assertIn("ordering agreement: 0% of 1 pairs", out.getvalue())
        self.assertIn("1 listing(s) held down by an earlier \"no\"",
                      out.getvalue())

class OrderingAgreement(unittest.TestCase):
    def test_better_verdict_with_higher_score_agrees(self):
        self.assertEqual(verdicts.ordering_agreement([(2, 60), (0, 50)]),
                         (1, 1))

    def test_equal_scores_count_half(self):
        self.assertEqual(verdicts.ordering_agreement([(2, 50), (0, 50)]),
                         (0.5, 1))

    def test_same_verdicts_are_not_compared(self):
        self.assertEqual(verdicts.ordering_agreement([(2, 60), (2, 50)]),
                         (0, 0))

    def test_reversed_order(self):
        self.assertEqual(verdicts.ordering_agreement(
            [(2, 40), (1, 50), (0, 60)]), (0, 3))

if __name__ == "__main__":
    unittest.main()
