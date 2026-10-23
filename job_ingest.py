#!/usr/bin/env python3
"""
job_ingest.py — fetch job-alert digest emails over IMAP,
write raw/*.txt files for screening. Each file is named for the folder's
UIDVALIDITY and the message's UID (job_uv<UIDVALIDITY>_uid<UID>.txt),
which together identify a message for as long as it exists.

With recency_days set (pipeline_config), the folder is searched with
SINCE, so the server leaves older digests out: they're never listed or
downloaded. The server folder is never touched (read-only guarantee).
"""
import email
import email.policy
import imaplib
import ipaddress
import os
import re
import ssl
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from html_text import html_to_text
from pipeline_common import configure_from_file, write_atomic

IMAP_TIMEOUT = 60  # seconds per socket operation
RAW_DIR = Path("raw")
# IMAP dates name the month in English, whatever the locale
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()

def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

def env_name(cfg, key, default):
    """Config names the env var that holds a credential; the credential
    itself stays out of the file. A value that isn't a valid variable
    name is probably the credential pasted in, so it's refused without
    being echoed."""
    name = cfg.get(key, default)
    if not (isinstance(name, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)):
        raise ValueError(f"{key} must be the NAME of an environment "
                         f"variable (e.g. {default}), not the value itself.")
    return name

def configure(cfg):
    """Set the mail settings from a config dict. main() passes
    pipeline_config.json and tests pass their own, so importing the
    module reads no files and no credentials. Raises ValueError for a
    setting that must stop the run: fail closed, no silent defaults."""
    global IMAP_HOST, IMAP_PORT, IMAP_STARTTLS, IMAP_TLS_VERIFY
    global IMAP_CAFILE, RECENCY_DAYS, USER_ENV, PASSWORD_ENV, MAIL_FOLDER
    IMAP_HOST = cfg.get("imap_host", os.getenv("EMAIL_IMAP_HOST", ""))
    # 993 is the standard implicit-TLS IMAP port
    IMAP_PORT = int(cfg.get("imap_port", os.getenv("EMAIL_IMAP_PORT", "993")))
    # True for servers that expect a plain connect upgraded with
    # STARTTLS; False (default) uses implicit TLS
    IMAP_STARTTLS = cfg.get("imap_starttls", False)
    # The server certificate is verified against the system CA store, or
    # only against imap_cafile when that is set (e.g. a local bridge's
    # exported certificate). imap_tls_verify=false skips verification and
    # is accepted for loopback hosts only, where traffic never leaves the
    # machine
    IMAP_TLS_VERIFY = cfg.get("imap_tls_verify", True)
    IMAP_CAFILE = cfg.get("imap_cafile")
    # Same knob jobs_screen.py reads: digests older than this are not
    # ingested. 0 or missing ingests everything.
    RECENCY_DAYS = int(cfg.get("recency_days", 0))
    USER_ENV = env_name(cfg, "email_user_env", "EMAIL_USER")
    PASSWORD_ENV = env_name(cfg, "email_password_env", "EMAIL_PASSWORD")
    MAIL_FOLDER = cfg.get("mail_folder", "")
    if not IMAP_TLS_VERIFY and not is_loopback(IMAP_HOST):
        raise ValueError(
            "imap_tls_verify=false is only accepted for a loopback "
            "imap_host (localhost, 127.0.0.1, ::1). To trust a "
            "self-signed certificate, set imap_cafile instead.")

# Until main() configures it, the module runs as for an empty config
configure({})

def tls_context():
    """TLS settings for the IMAP connection. Always passed explicitly:
    imaplib's own default (no context) does NOT verify certificates."""
    if IMAP_TLS_VERIFY:
        return ssl.create_default_context(cafile=IMAP_CAFILE)
    # Loopback only (checked by configure)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch_payload(data):
    """The message bytes from an imaplib FETCH response, or None.

    The payload arrives as a (header, bytes) tuple, but it isn't always
    data[0]: an unsolicited FETCH (another client changing flags) comes
    as plain bytes, and so does a field the server returns quoted or
    NIL instead of as a literal. Those entries are skipped.
    """
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2 \
                and isinstance(item[1], bytes):
            return item[1]
    return None

def search_criteria(now=None):
    """What to ask UID SEARCH for: every message, or with RECENCY_DAYS
    set, those that arrived SINCE the window's start. SINCE compares the
    arrival date, by whole days, so the window searched is one day
    longer; the screener cuts a stale digest by its Date header anyway.
    Checked 2026-10-23 against the bridge: at windows of 1 to 10 days,
    this missed no message the Date header put inside the window."""
    if not RECENCY_DAYS:
        return "(ALL)"
    start = ((now or datetime.now(timezone.utc))
             - timedelta(days=RECENCY_DAYS + 1)).date()
    return f"(SINCE {start.day:02d}-{MONTHS[start.month - 1]}-{start.year})"

def read_message(raw_email):
    """(subject, sender, date, body) from a raw message. Subject and
    sender are decoded; the date is the header as the email states it
    (the parsed form would add a weekday it may not have). The body is
    the text/plain part, or failing that the text/html one, decoded by
    the charset it declares (as UTF-8 when Python doesn't know that
    charset); None when the message has neither."""
    msg = email.message_from_bytes(raw_email, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = None
    if part is not None:
        try:
            body = part.get_content()
        except LookupError:  # unknown charset name
            body = part.get_payload(decode=True).decode("utf-8",
                                                        errors="replace")
    date = next((v for k, v in msg.raw_items() if k.lower() == "date"), "")
    return (str(msg.get("Subject", "")), str(msg.get("From", "")),
            date, body)

def digest_name(uidvalidity, uid):
    """A digest's filename. A UID is unique only within one UIDVALIDITY:
    when the server resets it (a local bridge rebuilding its cache, say),
    UIDs restart at 1, and a new digest under an old file's name would be
    skipped as a duplicate. With UIDVALIDITY in the name, a reset
    re-ingests the window instead, and the screener's dedup collapses
    the repeated listings."""
    return f"job_uv{uidvalidity}_uid{uid}.txt"

def folder_uidvalidity(mail):
    """The selected folder's UIDVALIDITY as a string, or None. RFC 3501
    requires the server to send it with the EXAMINE response."""
    _, values = mail.response("UIDVALIDITY")
    value = values[-1] if values else None
    if isinstance(value, bytes) and value.isdigit():
        return value.decode()
    return None

def adopt_legacy_names(uidvalidity):
    """Rename digests written before UIDVALIDITY was in the name
    (job_uid_<UID>.txt) under the folder's current UIDVALIDITY, so they
    aren't downloaded again. Runs on every ingest and finds nothing after
    the first. Returns how many files were renamed."""
    renamed = 0
    for f in RAW_DIR.glob("job_uid_*.txt"):
        uid = f.stem.removeprefix("job_uid_")
        target = RAW_DIR / digest_name(uidvalidity, uid)
        if uid.isdigit() and not target.exists():
            f.rename(target)
            renamed += 1
    return renamed

def digest_record(subject, sender, date, uid, text):
    """A digest file's full text: the metadata block, then the body with
    one field per line, which jobs_screen.py parses into listings."""
    return (f"# Metadata\nSubject: {subject}\nSender: {sender}\n"
            f"Date: {date}\nUID: {uid}\n---\n\n{text}")

def main():
    configure_from_file(configure)
    email_user = os.getenv(USER_ENV, "")
    password = os.getenv(PASSWORD_ENV, "")
    if not IMAP_HOST or not email_user or not password or not MAIL_FOLDER:
        print("[ERROR] imap_host/mail_folder (pipeline_config.json) and "
              f"{USER_ENV}/{PASSWORD_ENV} (environment) must all be set.")
        return 1

    RAW_DIR.mkdir(exist_ok=True)

    print(f"Connecting to {IMAP_HOST}:{IMAP_PORT}...")

    # Socket timeout so an unresponsive server fails the run instead of
    # hanging it (the scheduled wrapper retries failed runs)
    ctx = tls_context()
    if IMAP_STARTTLS:
        # Connect plain, upgrade to TLS before sending credentials
        mail = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.starttls(ssl_context=ctx)
    else:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ctx,
                                 timeout=IMAP_TIMEOUT)
    mail.login(email_user, password)

    print(f"Selecting folder: {MAIL_FOLDER}")
    # readonly=True issues EXAMINE: no flag changes, and close() can't
    # expunge messages another client marked \Deleted
    typ, data = mail.select(MAIL_FOLDER, readonly=True)
    if typ != "OK":
        # Fail closed: never widen scope to INBOX. If the folder is
        # missing, that's a configuration error to fix, not to route
        # around.
        print(f"[ERROR] Cannot select folder '{MAIL_FOLDER}': {data}")
        print("Create the folder in email or fix MAIL_FOLDER in config.")
        mail.logout()
        return 1
    # Fail closed: without UIDVALIDITY a file name can't tell this
    # folder's UIDs from those of an earlier UIDVALIDITY
    uidvalidity = folder_uidvalidity(mail)
    if uidvalidity is None:
        print(f"[ERROR] The server sent no UIDVALIDITY for '{MAIL_FOLDER}', "
              f"so UIDs can't identify its messages.")
        mail.logout()
        return 1
    renamed = adopt_legacy_names(uidvalidity)
    if renamed:
        print(f"Renamed {renamed} digest file(s) to include UIDVALIDITY "
              f"{uidvalidity}.")

    # Search BY UID to get stable identifiers directly (UIDs survive
    # deletions and moves; sequence numbers do not)
    criteria = search_criteria()
    typ, search_data = mail.uid('search', None, criteria)
    if typ != "OK":
        print(f"[ERROR] UID search failed: {search_data}")
        mail.logout()
        return 1

    print(f"Selected folder: {MAIL_FOLDER} ({data[0].decode()} messages)")
    uid_list = search_data[0].split()
    if not uid_list:
        print(f"No emails found in folder {criteria}.")
        mail.logout()
        return 0

    print(f"Found {len(uid_list)} emails to process {criteria}.")
    processed = duplicates = fetch_failures = no_body = 0

    for uid_bytes in uid_list:
        uid = uid_bytes.decode()

        # Duplicate check first: a UID whose file exists costs nothing.
        # UIDs, unlike sequence numbers, stay stable across deletions
        # and moves
        filepath = RAW_DIR / digest_name(uidvalidity, uid)
        if filepath.exists():
            duplicates += 1
            continue

        # Fetch by UID — one namespace end to end (uid search -> uid fetch).
        # BODY.PEEK[] returns the full message without setting \Seen.
        status, data = mail.uid('fetch', uid, '(BODY.PEEK[])')
        raw_email = fetch_payload(data) if status == "OK" else None
        if raw_email is None:
            print(f"Failed to fetch UID {uid}: {status}")
            fetch_failures += 1
            continue

        subject, sender, date_raw, body = read_message(raw_email)
        if body:
            # One field per line: the title / employer / location / View
            # quadruple jobs_screen.py parses. Plain text keeps its
            # lines; in HTML, table cells and block tags become line
            # breaks, so an HTML digest still yields one field per line
            clean_text = html_to_text(body)
        else:
            # Still written, with no listings, so the message isn't
            # downloaded again on every run; counted in the summary
            print(f"No body found: {subject[:50]}... ({filepath.name} "
                  f"written without listings)")
            no_body += 1
            clean_text = ""

        write_atomic(filepath, digest_record(subject, sender, date_raw,
                                             uid, clean_text))
        processed += 1
        print(f"Ingested UID {uid}: {subject[:60]}... ({filepath.name})")

    print(f"Processed {processed} new ({no_body} without a body), skipped "
          f"{duplicates} duplicate(s).")

    mail.close()
    mail.logout()
    if fetch_failures:
        print(f"[ERROR] {fetch_failures} message(s) failed to fetch.")
        return 1
    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
