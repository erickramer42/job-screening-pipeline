#!/usr/bin/env python3
"""
job_ingest.py — fetch job-alert digest emails over IMAP,
write raw/*.txt files for screening. Uses IMAP UIDs for stable filenames
that survive deletions/moves.

Digests whose Date header is older than recency_days (pipeline_config)
are skipped: header probe only, no body download, no file written. The
server folder is never touched (read-only guarantee), so old emails
cost one cheap header fetch per run until manually archived.
"""
import email
import imaplib
import ipaddress
import json
import os
import re
import ssl
import sys
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from html_text import html_to_text

# Configuration
CONFIG_FILE = Path("pipeline_config.json")

def load_config():
    """Fail closed: no config, no run. Prevents silent fallback to
    defaults — the same discipline as the missing-folder check."""
    if not CONFIG_FILE.exists():
        print("[ERROR] pipeline_config.json missing — copy "
              "pipeline_config.example.json and fill in your values.")
        return None
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

CFG = load_config()
if CFG is None:
    sys.exit(1)
IMAP_HOST = CFG.get("imap_host", os.getenv("EMAIL_IMAP_HOST", ""))
# 993 is the standard implicit-TLS IMAP port
IMAP_PORT = int(CFG.get("imap_port", os.getenv("EMAIL_IMAP_PORT", "993")))
# True for servers that expect a plain connect upgraded with STARTTLS;
# False (default) uses implicit TLS
IMAP_STARTTLS = CFG.get("imap_starttls", False)
# The server certificate is verified against the system CA store, or
# only against imap_cafile when that is set (e.g. a local bridge's
# exported certificate). imap_tls_verify=false skips verification and
# is accepted for loopback hosts only, where traffic never leaves the
# machine
IMAP_TLS_VERIFY = CFG.get("imap_tls_verify", True)
IMAP_CAFILE = CFG.get("imap_cafile")
IMAP_TIMEOUT = 60  # seconds per socket operation
# Same knob jobs_screen.py reads: digests older than this are not
# ingested. 0 or missing ingests everything.
RECENCY_DAYS = int(CFG.get("recency_days", 0))

def env_name(key, default):
    """Config names the env var that holds a credential; the credential
    itself stays out of the file. A value that isn't a valid variable
    name is probably the credential pasted in, so refuse it without
    echoing it."""
    name = CFG.get(key, default)
    if not (isinstance(name, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)):
        print(f"[ERROR] {key} must be the NAME of an environment variable "
              f"(e.g. {default}), not the value itself.")
        sys.exit(1)
    return name

USER_ENV = env_name("email_user_env", "EMAIL_USER")
PASSWORD_ENV = env_name("email_password_env", "EMAIL_PASSWORD")
EMAIL_USER = os.getenv(USER_ENV, "")
PASSWORD = os.getenv(PASSWORD_ENV, "")
MAIL_FOLDER = CFG.get("mail_folder", "")
RAW_DIR = Path("raw")

if not IMAP_HOST or not EMAIL_USER or not PASSWORD or not MAIL_FOLDER:
    print("[ERROR] imap_host/mail_folder (pipeline_config.json) and "
          f"{USER_ENV}/{PASSWORD_ENV} (environment) must all be set.")
    sys.exit(1)

def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

if not IMAP_TLS_VERIFY and not is_loopback(IMAP_HOST):
    print("[ERROR] imap_tls_verify=false is only accepted for a loopback "
          "imap_host (localhost, 127.0.0.1, ::1). To trust a self-signed "
          "certificate, set imap_cafile instead.")
    sys.exit(1)

def tls_context():
    """TLS settings for the IMAP connection. Always passed explicitly:
    imaplib's own default (no context) does NOT verify certificates."""
    if IMAP_TLS_VERIFY:
        return ssl.create_default_context(cafile=IMAP_CAFILE)
    # Loopback only (checked at startup)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def decode_mime_header(header):
    """Decode MIME encoded headers."""
    if not header:
        return ""
    decoded_parts = []
    for part, enc in decode_header(header):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
            except LookupError:  # unknown charset name
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts)

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

def digest_is_stale(mail, uid):
    """
    True when the email's Date header is older than RECENCY_DAYS.
    Header-only fetch (BODY.PEEK, so no Seen flag): the body is never
    downloaded for old digests. Unreadable/missing dates return False —
    age can't be verified, so the email is fetched and the screener's
    undated-digest path decides what happens to it.
    """
    if not RECENCY_DAYS:
        return False
    typ, data = mail.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (DATE)])')
    header = fetch_payload(data) if typ == "OK" else None
    if header is None:
        return False
    raw_date = email.message_from_bytes(header).get("Date", "")
    try:
        dt = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return False
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days > RECENCY_DAYS

def main():
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
    mail.login(EMAIL_USER, PASSWORD)

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

    # Search BY UID to get stable identifiers directly (UIDs survive
    # deletions and moves; sequence numbers do not)
    typ, search_data = mail.uid('search', None, "(ALL)")
    if typ != "OK":
        print(f"[ERROR] UID search failed: {search_data}")
        mail.logout()
        return 1

    print(f"Selected folder: {MAIL_FOLDER} ({data[0].decode()} messages)")
    uid_list = search_data[0].split()
    if not uid_list:
        print("No emails found in folder.")
        mail.logout()
        return 0

    print(f"Found {len(uid_list)} emails to process.")
    processed = duplicates = stale_skipped = fetch_failures = 0

    for uid_bytes in uid_list:
        uid = uid_bytes.decode()

        # Duplicate check first: a UID whose file exists costs nothing
        # (no header probe, no body fetch). UIDs, unlike sequence
        # numbers, stay stable across deletions and moves
        filepath = RAW_DIR / f"job_uid_{uid}.txt"
        if filepath.exists():
            duplicates += 1
            continue

        # Old digests are skipped before their bodies are downloaded.
        # Re-probed every run (header-only fetch) until the email is
        # manually archived on the server — deliberately cheap rather
        # than writing skip-tombstones to raw/
        if digest_is_stale(mail, uid):
            stale_skipped += 1
            continue

        # Fetch by UID — one namespace end to end (uid search -> uid fetch).
        # BODY.PEEK[] returns the full message without setting \Seen.
        status, data = mail.uid('fetch', uid, '(BODY.PEEK[])')
        raw_email = fetch_payload(data) if status == "OK" else None
        if raw_email is None:
            print(f"Failed to fetch UID {uid}: {status}")
            fetch_failures += 1
            continue

        msg_obj = email.message_from_bytes(raw_email)

        # Extract metadata
        subject_raw = msg_obj.get("Subject", "")
        from_raw = msg_obj.get("From", "")
        date_raw = msg_obj.get("Date", "")
        subject = decode_mime_header(subject_raw)
        sender = decode_mime_header(from_raw)

        # Extract body text (prefers text/plain, falls back to text/html)
        body = None
        if msg_obj.is_multipart():
            for part in msg_obj.walk():
                ctype = part.get_content_type()
                disposition = str(part.get("Content-Disposition"))
                if ctype == "text/plain" and "attachment" not in disposition:
                    try:
                        body = part.get_payload(decode=True).decode(
                            "utf-8", errors="replace")
                        break
                    except:
                        continue
                elif ctype == "text/html" and body is None:
                    try:
                        body = part.get_payload(decode=True).decode(
                            "utf-8", errors="replace")
                    except:
                        continue
        else:
            payload = msg_obj.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")

        if not body:
            print(f"No body found: {subject[:50]}...")
            continue

        # One field per line: the title / employer / location / View
        # quadruple jobs_screen.py parses. Plain text keeps its lines;
        # in the HTML fallback, table cells and block tags become line
        # breaks, so an HTML-only digest still yields one field per line
        clean_text = html_to_text(body)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("# Metadata\n")
            f.write(f"Subject: {subject}\n")
            f.write(f"Sender: {sender}\n")
            f.write(f"Date: {date_raw}\n")
            f.write(f"UID: {uid}\n")
            f.write("---\n\n")
            f.write(clean_text)

        processed += 1
        print(f"Ingested UID {uid}: {subject[:60]}... ({filepath.name})")

    print(f"Processed {processed} new, skipped {duplicates} duplicate(s) "
          f"and {stale_skipped} stale digest(s) "
          f"(recency_days={RECENCY_DAYS}).")

    mail.close()
    mail.logout()
    if fetch_failures:
        print(f"[ERROR] {fetch_failures} message(s) failed to fetch.")
        return 1
    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
    