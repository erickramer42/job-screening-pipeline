"""Tests for board_fetch.py: the board-file layout, pay parsing, and when
a posting's file is written, rewritten or archived. The board APIs are
replaced by fixtures; nothing goes over the network."""
import json
import tempfile
import unittest
import urllib.error
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

# The layout every live board file has (format 3). record_text must
# reproduce it byte for byte, or every file is rewritten on the next
# run; a deliberate change needs a FILE_FORMAT bump instead
EXPECTED_FILE = """\
# Metadata
Source: greenhouse/example
Company: Example Co
URL: https://example.com/1
Posted: 2026-09-01
Format: 3
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
                      "first_published": "2026-09-01T09:30:00-04:00",
                      "content": content}]}

class Layout(unittest.TestCase):
    def test_record_text_matches_the_file_format(self):
        self.assertEqual(bf.FILE_FORMAT, 3)
        self.assertEqual(bf.record_text(
            "greenhouse", "example", "Example Co", "https://example.com/1",
            "2026-09-01", "Senior Engineer",
            "Springfield ($120,000-$150,000)",
            "About the role\nBuild things."), EXPECTED_FILE)

    def test_no_description_no_marker(self):
        text = bf.record_text("lever", "example", "Example Co", "u", "",
                              "Senior Engineer", "Springfield", "")
        self.assertTrue(text.endswith("Springfield\nView\n"))
        self.assertNotIn(pipeline_common.DESC_MARKER, text)
        self.assertNotIn("Posted:", text)

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
                                "minValue": 1, "maxValue": 2},
                               "not a tier",
                               {"compensationType": "Salary",
                                "currencyCode": "USD", "interval": "1 YEAR",
                                "minValue": 130000, "maxValue": "n/a"}]}}]}
        (row,) = bf.flatten(board, "ashby", "example")
        self.assertEqual((row.pay, row.pay_min, row.pay_max),
                         ("$124,800-$145,600", 124800, 145600))
        self.assertIs(row.remote, False)

    def test_annual_range(self):
        table = {"year": 1, "hour": 2080}
        for tiers, expected in (
                ([(100000, 120000, "USD", "year")],
                 ("$100,000-$120,000", 100000, 120000)),
                ([(50, 60, "USD", "hour"), (110000, 130000, "USD", "year")],
                 ("$104,000-$130,000", 104000, 130000)),
                # A max that is missing, 0 or not a number is the min
                ([(130000, 0, "USD", "year")],
                 ("$130,000-$130,000", 130000, 130000)),
                ([(130000, None, "USD", "year")],
                 ("$130,000-$130,000", 130000, 130000)),
                ([(130000, "160k", "USD", "year")],
                 ("$130,000-$130,000", 130000, 130000)),
                ([(1, 2, "EUR", "year"), (1, 2, "USD", "month"),
                  ("100k", 2, "USD", "year"), (True, 2, "USD", "year"),
                  (1, 2, "USD", ["year"])],
                 ("", None, None)),
                ([], ("", None, None))):
            with self.subTest(tiers=tiers):
                self.assertEqual(bf.annual_range(tiers, table), expected)

    def test_greenhouse_arrangement_only_location_reads_the_offices(self):
        # With no metro cities configured every place passes, so the
        # offices are in name order
        offices = [{"name": "HQ", "location": "Springfield, ST"},
                   {"name": "Riverton office", "location": None},
                   {"name": "HQ", "location": "Springfield, ST"},
                   {"name": "Annex", "location": {"city": "Springfield"}},
                   {"name": None, "location": 5}]
        cases = [
            ("Hybrid", offices,
             ("Hybrid: Annex; Riverton office; Springfield, ST", None)),
            ("Hybrid; In-Office", offices[:1],
             ("Hybrid; In-Office: Springfield, ST", None)),
            ("Distributed", [{"location": "United States"}],
             ("Distributed: United States", True)),
            # A remote-labeled office counts only on "Distributed"
            ("In-Office", [{"name": "Remote US", "location": None},
                           {"location": "Springfield, ST"}],
             ("In-Office: Springfield, ST", None)),
            ("Hybrid", [{"name": "Remote US"}], ("Hybrid", None)),
            ("Distributed", [{"name": "Remote US"}],
             ("Distributed: Remote US", True)),
            # A name with a place, or a bare "Remote", is kept as it is
            ("Remote", offices, ("Remote", None)),
            ("Springfield", offices, ("Springfield", None)),
            ("Hybrid Springfield", offices, ("Hybrid Springfield", None)),
            ("Hybrid", [], ("Hybrid", None)),
            (None, offices, (None, None)),
        ]
        for name, offs, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(bf.greenhouse_location(name, offs), expected)

    def test_offices_in_the_area_come_first_and_the_rest_are_counted(self):
        bf.configure(CONFIG)
        self.addCleanup(bf.configure, {"metro_cities": [],
                                       "included_title_patterns": [],
                                       "excluded_keywords": []})
        def offices(*places):
            return [{"location": p} for p in places]
        cases = [
            ("Hybrid", offices("Zeta, XY", "Alpha, XY", "Beta, XY",
                               "Gamma, XY", "Springfield, ST"),
             ("Hybrid: Springfield, ST; Alpha, XY; Beta, XY; +2 more", None)),
            ("Distributed", offices("Toronto, ON", "Austin, XY", "Berlin",
                                    "United States", "Paris"),
             ("Distributed: United States; Austin, XY; Berlin; +2 more",
              True)),
            ("Hybrid", offices("Gamma, XY", "Alpha, XY", "Beta, XY"),
             ("Hybrid: Alpha, XY; Beta, XY; Gamma, XY", None)),
        ]
        for name, offs, expected in cases:
            with self.subTest(offices=offs):
                location, remote = bf.greenhouse_location(name, offs)
                self.assertEqual((location, remote), expected)
                # The filter's verdict is the one the full list gets
                full = f"{name}: {'; '.join(o['location'] for o in offs)}"
                self.assertEqual(bf.location_ok(location, remote, None),
                                 bf.location_ok(full, remote, None))

    def test_lever_description_pay_and_date(self):
        def posting(salary):
            return {"id": "l1", "text": "Senior Engineer",
                    "categories": {"location": "Springfield, ST",
                                   "allLocations": ["Springfield, ST"]},
                    "country": "US", "workplaceType": "hybrid",
                    "hostedUrl": "https://example.com/l1",
                    "createdAt": 1788220800000,  # 2026-09-01 UTC
                    "description": "<div>About Example Co &amp; us</div>",
                    "lists": [{"text": "What You'll Do",
                               "content": "<li>Build things</li>"
                                          "<li>Run them</li>"},
                              "not a list",
                              # Already holds an entity: decoded once
                              {"text": "Pay &amp; Perks",
                               "content": "<li>Equity</li>"}],
                    "additional": "<div>Pay is $1 &lt; $2</div>",
                    "salaryRange": salary}
        (row,) = bf.flatten([posting(
            {"min": 130000, "max": 160000, "currency": "USD",
             "interval": "per-year-salary"})], "lever", "example")
        self.assertEqual((row.pay, row.pay_min, row.pay_max, row.posted),
                         ("$130,000-$160,000", 130000, 160000, "2026-09-01"))
        self.assertEqual((row.remote, row.country), (False, "US"))
        self.assertEqual(bf.description_text("lever", row.raw_description),
                         "About Example Co & us\nWhat You'll Do\n"
                         "Build things\nRun them\nPay & Perks\nEquity\n"
                         "Pay is $1 < $2")
        for salary, pay in (
                ({"min": 60, "max": 70, "currency": "USD",
                  "interval": "per-hour-wage"}, "$124,800-$145,600"),
                ({"min": 90000, "currency": "USD",
                  "interval": "per-year-salary"}, "$90,000-$90,000"),
                # A max of 0 is missing, not a $0 top that the floor cuts
                ({"min": 130000, "max": 0, "currency": "USD",
                  "interval": "per-year-salary"}, "$130,000-$130,000"),
                ([130000, 160000], ""), ("$130k", ""),
                ({"min": 1, "max": 2, "currency": "GBP",
                  "interval": "per-year-salary"}, ""),
                ({"min": 1, "max": 2, "currency": "USD",
                  "interval": "one-time"}, ""),
                (None, "")):
            with self.subTest(salary=salary):
                (row,) = bf.flatten([posting(salary)], "lever", "example")
                self.assertEqual(row.pay, pay)

    def test_posted_date(self):
        for value, expected in (
                ("2026-09-01T09:30:00-04:00", "2026-09-01"),
                ("2026-09-01T23:59:59.000Z", "2026-09-01"),
                (1788220800000, "2026-09-01"),
                (None, ""), ("", ""), ("soon", ""), (True, ""),
                (10 ** 30, "")):
            with self.subTest(value=value):
                self.assertEqual(bf.posted_date(value), expected)

    def test_each_platform_reads_its_posted_date(self):
        boards = {
            "greenhouse": {"jobs": [{"id": 1, "first_published":
                                     "2026-09-01T00:00:00Z"}]},
            "ashby": {"jobs": [{"id": "a", "publishedAt":
                                "2026-09-01T00:00:00Z"}]},
            "smartrecruiters": {"content": [{"id": "s", "releasedDate":
                                             "2026-09-01T00:00:00.000Z"}]},
            "lever": [{"id": "l", "createdAt": 1788220800000}],
        }
        for platform, board in boards.items():
            with self.subTest(platform=platform):
                (row,) = bf.flatten(board, platform, "example")
                self.assertEqual(row.posted, "2026-09-01")

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

    def test_a_broken_boards_json_is_one_line_not_a_traceback(self):
        config = self.out.parent / "boards.json"
        for text, says in (('[{"platform": "greenhouse",', "boards.json:"),
                           ('{"platform": "greenhouse", "slug": "x"}',
                            "must be a JSON list")):
            with self.subTest(text=text):
                config.write_text(text, encoding="utf-8")
                with mock.patch.object(bf, "CONFIG", config), \
                        mock.patch.object(bf, "configure_from_file",
                                          lambda configure: configure(CONFIG)), \
                        mock.patch.object(bf, "fetch_board") as fetch, \
                        mock.patch("builtins.print") as printed:
                    self.assertEqual(bf.main(), 1)
                fetch.assert_not_called()
                (line,) = [c.args[0] for c in printed.call_args_list]
                self.assertTrue(line.startswith("[ERROR]"))
                self.assertIn(says, line)

    def test_only_a_404_points_at_the_slug(self):
        not_found = urllib.error.HTTPError("u", 404, "Not Found", None, None)
        self.assertIn("check the slug", bf.failure_text(not_found))
        for error in (TimeoutError("timed out"),
                      urllib.error.HTTPError("u", 503, "Unavailable", None,
                                             None),
                      KeyError("jobs")):
            with self.subTest(error=type(error).__name__):
                text = bf.failure_text(error)
                self.assertNotIn("slug", text)
                self.assertIn(type(error).__name__, text)

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
        self.assertEqual(job["posted"], "2026-09-01")

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

    def test_arrangement_only_locations_are_filtered_by_their_offices(self):
        def job(n, name, office):
            return {"id": n, "title": "Senior Engineer",
                    "location": {"name": name}, "absolute_url": "u",
                    "content": "", "offices": [{"location": office}]}
        self.sync({"jobs": [job(1, "Hybrid", "Springfield, ST"),
                            job(2, "Hybrid", "Elsewhere, XY"),
                            job(3, "Distributed", "United States"),
                            job(4, "Distributed", "Canada")]})
        lines = {f.name: f.read_text(encoding="utf-8").split("\n---\n")[1]
                 .split("\n")[3] for f in self.out.glob("*.txt")}
        self.assertEqual(lines, {
            "greenhouse_1.txt": "Hybrid: Springfield, ST",
            "greenhouse_3.txt": "Distributed: United States (Remote)"})

    def test_lever_pay_floor(self):
        def board(top):
            return [{"id": "l1", "text": "Senior Engineer",
                     "categories": {"location": "Springfield, ST"},
                     "hostedUrl": "u", "salaryRange": {
                         "min": 60000, "max": top, "currency": "USD",
                         "interval": "per-year-salary"}}]
        self.sync(board(80000), "lever")
        self.assertEqual(list(self.out.glob("*.txt")), [])
        self.sync(board(110000), "lever")
        self.assertIn("\nSpringfield, ST ($60,000-$110,000)\nView\n",
                      (self.out / "lever_l1.txt").read_text(encoding="utf-8"))

    def test_smartrecruiters_description_fetched_only_when_needed(self):
        def board(name, released="2026-09-01T00:00:00.000Z"):
            return {"content": [{"id": "abc", "name": name,
                                 "releasedDate": released, "location": {
                "city": "Springfield", "remote": False, "country": "us"}}]}
        detail = {"jobAd": {"sections": {"jobDescription": {
            "title": "Job Description", "text": "Build things."}}}}
        self.assertEqual(self.sync(board("Senior Engineer"),
                                   "smartrecruiters", detail).call_count, 1)
        # Unchanged listing: the stored description is kept, no request
        self.assertEqual(self.sync(board("Senior Engineer"),
                                   "smartrecruiters", detail).call_count, 0)
        self.assertEqual(self.write_calls.call_count, 1)
        # Re-released: the file gets the new date, and keeps its
        # description without a request
        self.assertEqual(self.sync(board("Senior Engineer",
                                         "2026-10-01T00:00:00.000Z"),
                                   "smartrecruiters", detail).call_count, 0)
        self.assertEqual(self.write_calls.call_count, 2)
        text = (self.out / "smartrecruiters_abc.txt").read_text(
            encoding="utf-8")
        self.assertIn("\nPosted: 2026-10-01\n", text)
        self.assertTrue(text.endswith("Job Description\nBuild things.\n"))
        # Edited listing: the description is fetched again
        self.assertEqual(self.sync(board("Lead Engineer",
                                         "2026-10-01T00:00:00.000Z"),
                                   "smartrecruiters", detail).call_count, 1)
        self.assertEqual(self.write_calls.call_count, 3)

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

    def test_a_pay_floor_that_isnt_a_number_stops_at_startup(self):
        with self.assertRaises(ValueError):
            bf.configure({**CONFIG, "pay_floor": "100k"})
        bf.configure({**CONFIG, "pay_floor": None})
        self.assertEqual(bf.PAY_FLOOR, 0)

if __name__ == "__main__":
    unittest.main()
