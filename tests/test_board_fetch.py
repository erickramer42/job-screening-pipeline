"""Tests for board_fetch.py: the board-file layout, pay parsing, and when
a posting's file is written, rewritten or archived. The board APIs are
replaced by fixtures; nothing goes over the network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import board_fetch as bf
import jobs_screen as js
import pipeline_common

CONFIG = {
    "metro_cities": ["springfield"],
    "remote_regions": ["ST"],
    "included_title_patterns": ["engineer"],
    "excluded_keywords": ["sales engineer"],
    "pay_floor": 100000,
}

# The layout every live board file has (format 2). record_text must
# reproduce it byte for byte, or every file is rewritten on the next
# run; a deliberate change needs a FILE_FORMAT bump instead
EXPECTED_FILE = """\
# Metadata
Source: greenhouse/example
Company: Example Co
URL: https://example.com/1
Title: Senior Engineer
Pay: $120,000-$150,000
Format: 2
---

Senior Engineer
Example Co
Springfield ($120,000-$150,000)
View

--- DESCRIPTION ---
About the role
Build things.
"""

def greenhouse_board(title="Senior Engineer", content=None):
    if content is None:
        content = ("&lt;p&gt;About the role&lt;/p&gt;&lt;p&gt;Build "
                   "things.&lt;/p&gt;")
    return {"jobs": [{"id": 1, "title": title,
                      "location": {"name": "Springfield"},
                      "absolute_url": "https://example.com/1",
                      "content": content}]}

class Layout(unittest.TestCase):
    def test_record_text_matches_the_file_format(self):
        self.assertEqual(bf.FILE_FORMAT, 2)
        self.assertEqual(bf.record_text(
            "greenhouse", "example", "Example Co", "https://example.com/1",
            "Senior Engineer", "$120,000-$150,000",
            "Springfield ($120,000-$150,000)",
            "About the role\nBuild things."), EXPECTED_FILE)

    def test_no_description_no_marker(self):
        text = bf.record_text("lever", "example", "Example Co", "u",
                              "Senior Engineer", "", "Springfield", "")
        self.assertTrue(text.endswith("Springfield\nView\n"))
        self.assertNotIn(pipeline_common.DESC_MARKER, text)

class Parsing(unittest.TestCase):
    def test_greenhouse_pay_from_the_transparency_block(self):
        html = ('&lt;div class="content-pay-transparency"&gt;$120,000 - '
                '$150,000 USD&lt;/div&gt;')
        self.assertEqual(bf.extract_greenhouse_pay(html), (120000, 150000))

    def test_greenhouse_pay_needs_the_block_and_sane_amounts(self):
        self.assertEqual(bf.extract_greenhouse_pay("$120,000"), (None, None))
        self.assertEqual(bf.extract_greenhouse_pay(
            '&lt;div class="content-pay-transparency"&gt;$5,000 bonus'),
            (None, None))
        self.assertEqual(bf.extract_greenhouse_pay(None), (None, None))

    def test_greenhouse_description_is_decoded_once(self):
        self.assertEqual(bf.description_text(
            "greenhouse", "&lt;p&gt;Use &amp;lt;b&amp;gt; tags&lt;/p&gt;"),
            "Use <b> tags")

    def test_smartrecruiters_sections_in_any_shape(self):
        detail = {"jobAd": {"sections": {
            "jobDescription": {"title": "Job Description",
                               "text": "<p>Build things.</p>"},
            "qualifications": [{"text": "Five years"}, "Python"],
            "companyDescription": {"text": "Boilerplate"}}}}
        self.assertEqual(bf.extract_smartrecruiters_description(detail),
                         "Job Description\nBuild things.\n\n"
                         "qualifications\nFive years\nPython")
        self.assertEqual(bf.extract_smartrecruiters_description({}), "")

    def test_ashby_usd_salary_only(self):
        board = {"jobs": [{"id": "a1", "title": "Senior Engineer",
                           "location": "Springfield", "jobUrl": "u",
                           "isRemote": False,
                           "compensation": {"summaryComponents": [
                               {"compensationType": "Salary",
                                "currencyCode": "USD", "interval": "1 HOUR",
                                "minValue": 60, "maxValue": 70},
                               {"compensationType": "Salary",
                                "currencyCode": "EUR", "interval": "1 YEAR",
                                "minValue": 1, "maxValue": 2}]}}]}
        (row,) = bf.flatten(board, "ashby", "example")
        self.assertEqual(row[4:7], ("$124,800-$145,600", 124800, 145600))

class SyncBoard(unittest.TestCase):
    """sync_board against a temporary raw_boards/, with the APIs mocked."""
    def setUp(self):
        bf.configure(CONFIG)
        self.addCleanup(bf.configure, {"metro_cities": [],
                                       "included_title_patterns": [],
                                       "excluded_keywords": []})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "raw_boards"
        self.out.mkdir()
        for name, value in (("OUT_DIR", self.out),
                            ("ARCHIVE_DIR", self.out / "archive")):
            patcher = mock.patch.object(bf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.writes = mock.patch.object(
            bf, "write_atomic", side_effect=pipeline_common.write_atomic)
        self.write_calls = self.writes.start()
        self.addCleanup(self.writes.stop)

    def sync(self, board, platform="greenhouse", detail=None):
        with mock.patch.object(bf, "fetch_board", return_value=board), \
                mock.patch.object(bf, "fetch_smartrecruiters_detail",
                                  return_value=detail) as fetch_detail, \
                mock.patch("builtins.print") as printed:
            bf.sync_board({"platform": platform, "slug": "example",
                           "company": "Example Co"})
        self.printed = [c.args[0] for c in printed.call_args_list]
        return fetch_detail

    def run_main(self, boards):
        """main() with this boards.json; every board returns one posting."""
        config = self.out.parent / "boards.json"
        config.write_text(json.dumps(boards), encoding="utf-8")
        with mock.patch.object(bf, "CONFIG", config), \
                mock.patch.object(bf, "configure_from_file",
                                  lambda configure: configure(CONFIG)), \
                mock.patch.object(bf, "fetch_board",
                                  return_value=greenhouse_board()), \
                mock.patch("builtins.print"):
            return bf.main()

    def other_board_file(self, name="greenhouse_9.txt",
                         source="greenhouse/another"):
        f = self.out / name
        f.write_text(f"# Metadata\nSource: {source}\n", encoding="utf-8")
        return f

    def test_unchanged_posting_is_not_rewritten(self):
        self.sync(greenhouse_board())
        self.sync(greenhouse_board())
        self.assertEqual(self.write_calls.call_count, 1)

    def test_edited_posting_is_rewritten(self):
        self.sync(greenhouse_board())
        self.sync(greenhouse_board(title="Senior Software Engineer"))
        self.assertEqual(self.write_calls.call_count, 2)
        text = (self.out / "greenhouse_1.txt").read_text(encoding="utf-8")
        self.assertIn("\nSenior Software Engineer\nExample Co\n", text)

    def test_truncated_file_is_repaired(self):
        self.sync(greenhouse_board())
        f = self.out / "greenhouse_1.txt"
        f.write_text(f.read_text(encoding="utf-8")[:60], encoding="utf-8")
        self.sync(greenhouse_board())
        self.assertIn("Build things.", f.read_text(encoding="utf-8"))

    def test_written_file_parses_in_the_screener(self):
        self.sync(greenhouse_board())
        with mock.patch.object(js, "SOURCE_DIRS", [(self.out, "*.txt")]):
            (job,), _ = js.parse_jobs()
        self.assertEqual((job["title"], job["employer"], job["location"],
                          job["url"]),
                         ("Senior Engineer", "Example Co", "Springfield",
                          "https://example.com/1"))
        self.assertEqual(job["description"], "About the role\nBuild things.")

    def test_closed_posting_is_archived_and_other_boards_kept(self):
        self.sync(greenhouse_board())
        other = self.other_board_file()
        self.sync({"jobs": []})
        self.assertTrue((self.out / "archive" / "greenhouse_1.txt").exists())
        self.assertTrue(other.exists())

    def test_the_board_line_counts_what_was_fetched(self):
        # An empty board and a filtered one both have 0 live postings
        self.sync({"jobs": []})
        self.assertTrue(self.printed[-1].startswith(
            "  Example Co: 0 fetched, 0 new"))
        self.sync(greenhouse_board(title="Sales Engineer"))
        self.assertTrue(self.printed[-1].startswith(
            "  Example Co: 1 fetched, 0 new"))

    def test_files_of_a_board_no_longer_configured_are_archived(self):
        other = self.other_board_file()
        no_source = self.out / "notes.txt"
        no_source.write_text("Senior Engineer\n", encoding="utf-8")
        board = {"platform": "greenhouse", "slug": "example",
                 "company": "Example Co"}
        self.assertEqual(self.run_main([board]), 0)
        self.assertTrue((self.out / "greenhouse_1.txt").exists())
        self.assertTrue((self.out / "archive" / other.name).exists())
        self.assertTrue(no_source.exists())

    def test_nothing_is_archived_as_unconfigured_beside_a_bad_entry(self):
        # The malformed entry might be the board those files belong to
        other = self.other_board_file()
        board = {"platform": "greenhouse", "slug": "example",
                 "company": "Example Co"}
        self.assertEqual(self.run_main([board, {"company": "Another"}]), 1)
        self.assertTrue((self.out / "greenhouse_1.txt").exists())
        self.assertTrue(other.exists())

    def test_filters(self):
        board = {"jobs": [
            {"id": 2, "title": "Sales Engineer", "location":
             {"name": "Springfield"}, "absolute_url": "u", "content": ""},
            {"id": 3, "title": "Senior Engineer", "location":
             {"name": "Elsewhere"}, "absolute_url": "u", "content": ""},
            {"id": 4, "title": "Senior Engineer", "location":
             {"name": "Springfield"}, "absolute_url": "u",
             "content": '&lt;div class="content-pay-transparency"&gt;'
                        '$60,000 - $80,000&lt;/div&gt;'}]}
        self.sync(board)
        self.assertEqual(list(self.out.glob("*.txt")), [])

    def test_smartrecruiters_description_fetched_only_when_needed(self):
        def board(name):
            return {"content": [{"id": "abc", "name": name, "location": {
                "city": "Springfield", "remote": False, "country": "us"}}]}
        detail = {"jobAd": {"sections": {"jobDescription": {
            "title": "Job Description", "text": "Build things."}}}}
        self.assertEqual(self.sync(board("Senior Engineer"),
                                   "smartrecruiters", detail).call_count, 1)
        # Unchanged listing: the stored description is kept, no request
        self.assertEqual(self.sync(board("Senior Engineer"),
                                   "smartrecruiters", detail).call_count, 0)
        self.assertEqual(self.write_calls.call_count, 1)
        # Edited listing: the description is fetched again
        self.assertEqual(self.sync(board("Lead Engineer"),
                                   "smartrecruiters", detail).call_count, 1)
        self.assertEqual(self.write_calls.call_count, 2)

class Configure(unittest.TestCase):
    def tearDown(self):
        bf.configure({"metro_cities": [], "included_title_patterns": [],
                      "excluded_keywords": []})

    def test_missing_required_key(self):
        with self.assertRaises(KeyError):
            bf.configure({"metro_cities": [], "excluded_keywords": []})

    def test_empty_title_list_fetches_every_title(self):
        bf.configure({**CONFIG, "included_title_patterns": []})
        self.assertTrue(bf.TITLE_RE.search("Anything"))

if __name__ == "__main__":
    unittest.main()
