"""Tests for verdicts.py: the listing key, the "Title | Employer" split,
the template round trip, and ordering agreement. Run from the repo root
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
