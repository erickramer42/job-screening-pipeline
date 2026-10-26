"""Tests for slug_probe.py: slug guesses, the input file, and telling
"no board" apart from "couldn't ask". The network is mocked."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import slug_probe

def answer(payload):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return resp

class SlugVariants(unittest.TestCase):
    def test_plain_name_first_and_corporate_words_only_as_words(self):
        self.assertEqual(slug_probe.slug_variants("Cisco")[0], "cisco")
        self.assertEqual(slug_probe.slug_variants("Costco")[0], "costco")
        self.assertIn("acme", slug_probe.slug_variants("Acme Technologies"))

class Candidates(unittest.TestCase):
    def test_names_come_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.txt"
            path.write_text("Acme\n\n Beta Co \n", encoding="utf-8")
            with mock.patch.object(sys, "argv", ["slug_probe.py", str(path)]):
                self.assertEqual(slug_probe.candidates(), ["Acme", "Beta Co"])

    def test_auto_mode_takes_ranked_employers_scoring_60_or_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "ranked_2026-01-02.json").write_text(json.dumps({
                "ranked": [{"employer": "Acme", "score": 80},
                           {"employer": "ACME", "score": 70},
                           {"employer": "Not Available", "score": 65},
                           # Capped by score_caps, as a staffing agency is
                           {"employer": "Staffing Co", "score": 50},
                           {"employer": "Beta", "score": 60}],
                "rejected": []}), encoding="utf-8")
            with mock.patch.object(sys, "argv", ["slug_probe.py"]), \
                    mock.patch.object(slug_probe, "RUNS_DIR", out), \
                    mock.patch("builtins.print"):
                self.assertEqual(slug_probe.candidates(), ["Acme", "Beta"])

    def test_a_missing_file_stops_rather_than_probing_other_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["slug_probe.py", str(Path(tmp) / "targest.txt")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch("builtins.print"), \
                    self.assertRaises(SystemExit) as cm:
                slug_probe.candidates()
        self.assertEqual(cm.exception.code, 1)

class Probe(unittest.TestCase):
    def test_jobs_mean_a_board(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=answer({"jobs": [{"id": 1}]})):
            self.assertTrue(slug_probe.probe("acme", "greenhouse"))

    def test_no_jobs_or_a_4xx_mean_no_board(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=answer({"jobs": []})):
            self.assertFalse(slug_probe.probe("acme", "greenhouse"))
        not_found = urllib.error.HTTPError("u", 404, "Not Found", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=not_found):
            self.assertFalse(slug_probe.probe("acme", "greenhouse"))

    def test_the_board_names_its_company_where_the_platform_can(self):
        greenhouse = [{"id": 1, "company_name": "Acme Robotics"}]
        smartrecruiters = [{"id": "a", "company": {"name": "Acme"}}]
        self.assertEqual(slug_probe.board_name("greenhouse", greenhouse),
                         "Acme Robotics")
        self.assertEqual(slug_probe.board_name("smartrecruiters",
                                               smartrecruiters), "Acme")
        self.assertIsNone(slug_probe.board_name("ashby", [{"id": "a"}]))

    def test_a_different_company_name_is_flagged(self):
        self.assertTrue(slug_probe.name_note("Acme", "ACME, Inc.")
                        .startswith("the board is"))
        self.assertTrue(slug_probe.name_note("Initech", "Initech Labs")
                        .startswith("the board is"))
        self.assertTrue(slug_probe.name_note("Initech", "Acme")
                        .startswith("CHECK"))
        self.assertIn("check the board", slug_probe.name_note("Acme", None))

    def test_an_outage_is_not_no_board(self):
        for error in (urllib.error.URLError("down"),
                      urllib.error.HTTPError("u", 503, "Unavailable", None,
                                             None),
                      # Throttled: the board may well exist
                      urllib.error.HTTPError("u", 429, "Too Many Requests",
                                             None, None),
                      TimeoutError()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(OSError):
                    slug_probe.probe("acme", "greenhouse")

if __name__ == "__main__":
    unittest.main()
