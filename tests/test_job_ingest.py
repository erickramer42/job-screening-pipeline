"""Tests for job_ingest.py: reading a message's body and headers, the
digest file it writes (and that the screener parses it), and the
settings checks. No mail server is involved."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

import job_ingest as ji
import jobs_screen as js
from html_text import html_to_text

def raw_message(body, content_type="text/plain; charset=utf-8",
                encoding="8bit", subject="Job alerts",
                date="11 Oct 2026 01:19:30 -0400"):
    head = (f"Subject: {subject}\r\nFrom: Alerts <alerts@example.com>\r\n"
            f"Date: {date}\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Transfer-Encoding: {encoding}\r\n\r\n")
    return head.encode("ascii") + body

class ReadMessage(unittest.TestCase):
    def test_declared_charset_is_honoured(self):
        raw = raw_message("Senior Engineer\r\nCafé Co\r\n".encode("latin-1"),
                          "text/plain; charset=iso-8859-1")
        self.assertIn("Café Co", ji.read_message(raw)[3])

    def test_unknown_charset_falls_back_to_utf8(self):
        raw = raw_message("Café Co\r\n".encode("utf-8"),
                          "text/plain; charset=x-no-such-charset")
        self.assertIn("Café Co", ji.read_message(raw)[3])

    def test_headers(self):
        raw = raw_message(b"x", subject="=?utf-8?q?Caf=C3=A9_alerts?=")
        subject, sender, date, _ = ji.read_message(raw)
        self.assertEqual(subject, "Café alerts")
        self.assertEqual(sender, "Alerts <alerts@example.com>")
        # As the email states it: no weekday is added
        self.assertEqual(date, "11 Oct 2026 01:19:30 -0400")

    def test_plain_part_preferred_over_html(self):
        body = (b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                b"plain text\r\n--b\r\nContent-Type: text/html; "
                b"charset=utf-8\r\n\r\n<p>html text</p>\r\n--b--\r\n")
        raw = raw_message(body, 'multipart/alternative; boundary="b"')
        self.assertIn("plain text", ji.read_message(raw)[3])

    def test_no_text_part(self):
        body = (b"--b\r\nContent-Type: application/pdf\r\n\r\n%PDF\r\n"
                b"--b--\r\n")
        raw = raw_message(body, 'multipart/mixed; boundary="b"')
        self.assertIsNone(ji.read_message(raw)[3])

class DigestContract(unittest.TestCase):
    """A digest ingest writes must parse into listings in the screener."""
    def test_html_digest_parses_in_the_screener(self):
        sent = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
        html = ("<table><tr><td>Senior Engineer</td><td>Acme</td>"
                "<td>Springfield</td><td><a href='x'>View</a></td></tr>"
                "<tr><td>Data Engineer</td><td>Beta &amp; Co</td>"
                "<td>Remote</td><td><a href='y'>View</a></td></tr></table>")
        raw = raw_message(html.encode(), "text/html; charset=utf-8",
                          date=sent)
        subject, sender, date, body = ji.read_message(raw)
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp) / "raw"
            raw_dir.mkdir()
            (raw_dir / "job_uid_7.txt").write_text(
                ji.digest_record(subject, sender, date, "7",
                                 html_to_text(body)), encoding="utf-8")
            js.configure({"excluded_employers": [],
                          "excluded_title_patterns": [],
                          "metro_cities": [], "recency_days": 30})
            with mock.patch.object(js, "SOURCE_DIRS",
                                   [(raw_dir, "job_*.txt")]):
                jobs = js.parse_jobs()
        self.assertEqual(sorted((j["title"], j["employer"], j["location"])
                                for j in jobs),
                         [("Data Engineer", "Beta & Co", "Remote"),
                          ("Senior Engineer", "Acme", "Springfield")])
        self.assertFalse(any(j["_stale"] or j["_undated"] for j in jobs))

class Configure(unittest.TestCase):
    def tearDown(self):
        ji.configure({})

    def test_a_pasted_credential_is_refused_without_echoing_it(self):
        with self.assertRaises(ValueError) as cm:
            ji.configure({"email_password_env": "hunter2!"})
        self.assertNotIn("hunter2", str(cm.exception))

    def test_unverified_tls_only_for_loopback(self):
        with self.assertRaises(ValueError):
            ji.configure({"imap_host": "imap.example.com",
                          "imap_tls_verify": False})
        ji.configure({"imap_host": "127.0.0.1", "imap_tls_verify": False})
        self.assertFalse(ji.tls_context().check_hostname)

    def test_verified_tls_by_default(self):
        ji.configure({"imap_host": "imap.example.com"})
        self.assertTrue(ji.tls_context().check_hostname)

    def test_is_loopback(self):
        for host, expected in (("localhost", True), ("127.0.0.1", True),
                               ("::1", True), ("imap.example.com", False),
                               ("10.0.0.1", False)):
            with self.subTest(host=host):
                self.assertEqual(ji.is_loopback(host), expected)

if __name__ == "__main__":
    unittest.main()
