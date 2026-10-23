"""Tests for the shared helpers: html_text, location_filter and
pipeline_common."""
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline_common
from html_text import html_to_text
from location_filter import location_matcher
from pipeline_common import (configure_from_file, parse_email_date,
                             write_atomic)

class HtmlToText(unittest.TestCase):
    def test_cells_and_blocks_become_lines(self):
        self.assertEqual(html_to_text(
            "<tr><td>Senior Engineer</td><td>Acme</td></tr><p>View</p>"),
            "Senior Engineer\nAcme\nView")

    def test_entities_are_decoded_after_tags_are_stripped(self):
        self.assertEqual(html_to_text("<p>Use &lt;b&gt; for bold</p>"),
                         "Use <b> for bold")

    def test_whitespace_and_nbsp_collapse(self):
        self.assertEqual(html_to_text("About the&nbsp;role\n\n  next "),
                         "About the role\nnext")

    def test_script_and_style_are_dropped(self):
        self.assertEqual(html_to_text(
            "<style>p {}</style><script>x()</script><p>Text</p>"), "Text")

class LocationMatcher(unittest.TestCase):
    def setUp(self):
        self.ok = location_matcher(["springfield"], ["ST"])

    def test_metro_city(self):
        self.assertTrue(self.ok("West Springfield, ST"))
        self.assertFalse(self.ok("Elsewhere"))

    def test_city_names_are_plain_whole_words(self):
        self.assertFalse(self.ok("Springfieldtown"))
        ok = location_matcher(["St. Louis", "C++ City"])
        self.assertTrue(ok("St. Louis, MO"))
        self.assertTrue(ok("Remote or C++ City"))
        self.assertFalse(ok("Stu Louis"))
        self.assertFalse(ok("C City"))

    def test_remote_needs_the_us(self):
        self.assertTrue(self.ok("Remote"))
        self.assertTrue(self.ok("Remote - US"))
        self.assertTrue(self.ok("Remote - ST"))
        self.assertFalse(self.ok("Remote - Canada"))
        self.assertFalse(self.ok("Remote (EMEA)"))

    def test_structured_fields_win(self):
        self.assertTrue(self.ok("Anywhere City", remote=True, country="US"))
        self.assertFalse(self.ok("Anywhere City", remote=True, country="DE"))
        self.assertFalse(self.ok("Elsewhere", remote=False, country="US"))

    def test_empty_city_list_matches_everything(self):
        self.assertTrue(location_matcher([])("Elsewhere"))

class EmailDate(unittest.TestCase):
    def test_zone_is_kept(self):
        dt = parse_email_date("11 Oct 2026 01:19:30 -0400")
        self.assertEqual(dt.utcoffset().total_seconds(), -4 * 3600)

    def test_no_zone_means_utc(self):
        self.assertEqual(parse_email_date(
            "Sun, 11 Oct 2026 01:19:30 -0000").utcoffset().total_seconds(), 0)

    def test_unreadable_is_none(self):
        for value in ("", "not a date", None):
            with self.subTest(value=value):
                self.assertIsNone(parse_email_date(value))

class ConfigureFromFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "pipeline_config.json"
        patcher = mock.patch.object(pipeline_common, "CONFIG_FILE",
                                    self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_good_config_is_passed_on(self):
        self.config.write_text('{"metro_cities": []}', encoding="utf-8")
        seen = []
        configure_from_file(seen.append)
        self.assertEqual(seen, [{"metro_cities": []}])

    def test_config_mistakes_stop_with_one_line_naming_the_file(self):
        def configure(cfg):
            re.compile(cfg["pattern"])
        for name, text in (("JSON syntax", '{"pattern": "a",}'),
                           ("missing key", "{}"),
                           ("bad regex", '{"pattern": "("}')):
            with self.subTest(name):
                self.config.write_text(text, encoding="utf-8")
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        self.assertRaises(SystemExit) as cm:
                    configure_from_file(configure)
                self.assertEqual(cm.exception.code, 1)
                lines = out.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("pipeline_config.json", lines[0])

class WriteAtomic(unittest.TestCase):
    def test_replaces_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            path.write_text("old", encoding="utf-8")
            write_atomic(path, "new\ntext\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "new\ntext\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             ["out.txt"])

if __name__ == "__main__":
    unittest.main()
