"""Tests for job_ingest.py: reading a message's body and headers, the
digest file it writes (and that the screener parses it), file names,
a run against a stand-in mailbox, and the settings checks. No mail
server is involved."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

import job_ingest as ji
import jobs_screen as js

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

    def test_plain_text_keeps_angle_brackets_and_entities(self):
        raw = raw_message(b"Senior Engineer\r\nPay < $100k, bonus > $5k\r\n"
                          b"R&amp;D Co\r\nView <https://example.com/1>\r\n")
        self.assertEqual(ji.read_message(raw)[3],
                         "Senior Engineer\nPay < $100k, bonus > $5k\n"
                         "R&amp;D Co\nView <https://example.com/1>")

    def test_html_is_converted_to_one_field_per_line(self):
        raw = raw_message(b"<p>Senior Engineer</p><p>R&amp;D Co</p>",
                          "text/html; charset=utf-8")
        self.assertEqual(ji.read_message(raw)[3], "Senior Engineer\nR&D Co")

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
        subject, sender, date, text = ji.read_message(raw)
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp) / "raw"
            raw_dir.mkdir()
            (raw_dir / ji.digest_name("41", "7")).write_text(
                ji.digest_record(subject, sender, date, "7", text),
                encoding="utf-8")
            js.configure({"excluded_employers": [],
                          "excluded_title_patterns": [],
                          "metro_cities": [], "recency_days": 30})
            with mock.patch.object(js, "SOURCE_DIRS",
                                   [(raw_dir, "job_*.txt")]):
                jobs, unparsed = js.parse_jobs()
        self.assertEqual(sorted((j["title"], j["employer"], j["location"])
                                for j in jobs),
                         [("Data Engineer", "Beta & Co", "Remote"),
                          ("Senior Engineer", "Acme", "Springfield")])
        self.assertFalse(any(j["_stale"] or j["_undated"] for j in jobs))
        self.assertEqual(unparsed, [])

class FakeMailbox:
    """Stands in for imaplib.IMAP4_SSL: one folder of {uid: raw message},
    and the commands it was sent."""
    def __init__(self, messages, uidvalidity=b"41"):
        self.messages, self.uidvalidity = messages, uidvalidity
        self.commands = []

    def __call__(self, *args, **kwargs):  # the constructor
        return self

    def __getattr__(self, name):  # login, close, logout, store, ...
        return lambda *args, **kwargs: self.commands.append((name, args))

    def select(self, folder, readonly=False):
        self.commands.append(("select", (folder, readonly)))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.uidvalidity]

    def uid(self, command, *args):
        self.commands.append(("uid " + command, args))
        if command == "search":
            return "OK", [" ".join(self.messages).encode()]
        return "OK", [(b"1 (BODY[] {1}", self.messages[args[0]]), b")"]

class Run(unittest.TestCase):
    """main() against FakeMailbox, writing to a temporary raw/."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw = Path(tmp.name) / "raw"
        sent = format_datetime(datetime.now(timezone.utc) - timedelta(days=1))
        self.message = raw_message(
            b"Senior Engineer\r\nAcme\r\nSpringfield\r\nView\r\n", date=sent)
        cfg = {"imap_host": "imap.example.com", "mail_folder": "Jobs",
               "recency_days": 30}
        for target, name, value in (
                (ji, "RAW_DIR", self.raw),
                (ji, "configure_from_file", lambda c: c(cfg))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"EMAIL_USER": "user",
                                             "EMAIL_PASSWORD": "secret"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(ji.configure, {})

    def run_main(self, mailbox):
        with mock.patch.object(ji.imaplib, "IMAP4_SSL", mailbox), \
                mock.patch("builtins.print"):
            return ji.main()

    def test_new_digests_are_named_by_uidvalidity_and_uid(self):
        self.raw.mkdir()
        # Written before UIDVALIDITY was in the name: renamed, not fetched
        (self.raw / "job_uid_5.txt").write_text("old", encoding="utf-8")
        mailbox = FakeMailbox({"5": self.message, "6": self.message})
        self.assertEqual(self.run_main(mailbox), 0)
        self.assertEqual(sorted(p.name for p in self.raw.iterdir()),
                         ["job_uv41_uid5.txt", "job_uv41_uid6.txt"])
        self.assertEqual((self.raw / "job_uv41_uid5.txt").read_text(
            encoding="utf-8"), "old")
        self.assertNotIn(("uid fetch", ("5", "(BODY.PEEK[])")),
                         mailbox.commands)

    def test_the_server_leaves_old_digests_out(self):
        mailbox = FakeMailbox({"6": self.message})
        self.run_main(mailbox)
        (search,) = [args for name, args in mailbox.commands
                     if name == "uid search"]
        self.assertRegex(search[1], r"^\(SINCE \d\d-[A-Z][a-z]{2}-\d{4}\)$")

    def test_since_names_the_day_before_the_window_in_english(self):
        ji.configure({"recency_days": 30})
        now = datetime(2026, 3, 31, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(ji.search_criteria(now), "(SINCE 28-Feb-2026)")
        ji.configure({})
        self.assertEqual(ji.search_criteria(now), "(ALL)")

    def test_the_mailbox_is_only_read(self):
        mailbox = FakeMailbox({"6": self.message})
        self.run_main(mailbox)
        names = {name for name, _ in mailbox.commands}
        self.assertIn(("select", ("Jobs", True)), mailbox.commands)
        self.assertFalse(names & {"store", "uid store", "expunge",
                                  "uid expunge", "copy", "uid copy",
                                  "uid move", "append", "delete"})
        self.assertTrue(all("PEEK" in args[1] for name, args in
                            mailbox.commands if name == "uid fetch"))

    def test_no_uidvalidity_stops_the_run(self):
        mailbox = FakeMailbox({"6": self.message}, uidvalidity=None)
        self.assertEqual(self.run_main(mailbox), 1)
        self.assertEqual(list(self.raw.iterdir()), [])

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
