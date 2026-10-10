#!/usr/bin/env python3
"""
job_ingest.py — fetch job-alert digest emails over IMAP,
write raw/*.txt files for screening. Uses IMAP UIDs for stable filenames
that survive deletions/moves.
"""
import os
import sys
import imaplib
import email
from email.header import decode_header
from pathlib import Path
from datetime import datetime
import hashlib
import re
import html as html_mod
import json

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
IMAP_TIMEOUT = 60  # seconds per socket operation
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
PROCESSED_DIR = Path("processed")

if not IMAP_HOST or not EMAIL_USER or not PASSWORD or not MAIL_FOLDER:
    print("[ERROR] imap_host/mail_folder (pipeline_config.json) and "
          f"{USER_ENV}/{PASSWORD_ENV} (environment) must all be set.")
    sys.exit(1)

def decode_mime_header(header):
    """Decode MIME encoded headers."""
    if not header:
        return ""
    decoded_parts = []
    for part, enc in decode_header(header):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
            except:
                decoded_parts.append(part.decode("utf-8", errors="replace"))
        else:
            decoded_parts.append(str(part))
    return "".join(decoded_parts)

def extract_job_text(email_content):
    """
    Convert email body to text. For plaintext emails, preserves line structure
    (tabs may appear as spaces per Python's text parsing, but the 4-line
    quadruple format survives intact). For HTML, strips tags and unescapes.
    
    IMPORTANT: This script always prefers text/plain over text/html, so
    HTML extraction path is fallback-only. Plaintext quad format is what
    jobs_screen.py expects.
    """
    if isinstance(email_content, bytes):
        try:
            text = email_content.decode("utf-8", errors="replace")
        except Exception:
            text = email_content.decode("latin-1", errors="replace")
    else:
        text = email_content

    # Kill style/script blocks entirely (keeps CSS out of output)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)
    text = re.sub(r"(?is)<script.*?</script>", " ", text)

    # Structural tags become delimiters BEFORE stripping remaining tags
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</td>", "\t", text)
    text = re.sub(r"(?i)</tr>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|table)>", "\n", text)

    # Strip remaining tags, unescape entities (&amp; -> &)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)

    # Collapse intra-line whitespace; keep the newlines we just made
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)

def uid_to_filename(uid):
    """Convert IMAP UID to safe filename (stable across email deletions)."""
    return f"job_uid_{uid}.txt"

def is_duplicate(filepath):
    """Check if we already processed this UID."""
    return filepath.exists()

def job_id_hash(subject, sender, date_str):
    """Build stable ID from subject+sender+date so different digests
    get different IDs even if title/sender are identical."""
    data = f"{subject}|{sender}|{date_str}"
    return hashlib.md5(data.encode()).hexdigest()[:12]

def main():
    RAW_DIR.mkdir(exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)

    print(f"Connecting to {IMAP_HOST}:{IMAP_PORT}...")

    # Socket timeout so an unresponsive server fails the run instead of
    # hanging it (the scheduled wrapper retries failed runs)
    if IMAP_STARTTLS:
        # Connect plain, upgrade to TLS before sending credentials
        mail = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
        mail.starttls()
        mail.login(EMAIL_USER, PASSWORD)
    else:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT)
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
    processed = fetch_failures = 0

    for uid_bytes in uid_list:
        uid = uid_bytes.decode()

        # Fetch by UID — one namespace end to end (uid search -> uid fetch).
        # BODY.PEEK[] returns the full message without setting \Seen.
        status, data = mail.uid('fetch', uid, '(BODY.PEEK[])')
        if status != "OK" or not data or not data[0]:
            print(f"Failed to fetch UID {uid}: {status}")
            fetch_failures += 1
            continue

        raw_email = data[0][1]
        msg_obj = email.message_from_bytes(raw_email)

        # Extract metadata
        subject_raw = msg_obj.get("Subject", "")
        from_raw = msg_obj.get("From", "")
        date_raw = msg_obj.get("Date", "")
        subject = decode_mime_header(subject_raw)
        sender = decode_mime_header(from_raw)

        # Build job ID from subject+sender+date for cross-digest
        # uniqueness (Bug #2 fix from CLAUDE.md known-issues)
        job_id = job_id_hash(subject, sender, date_raw)

        filename = uid_to_filename(uid)
        filepath = RAW_DIR / filename

        if is_duplicate(filepath):
            print(f"Skipping duplicate UID {uid} ({subject[:50]}...)")
            continue

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

        # Clean and extract job text
        clean_text = extract_job_text(body)

        # Write to file
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Metadata\n")
            f.write(f"Subject: {subject}\n")
            f.write(f"Sender: {sender}\n")
            f.write(f"Date: {date_raw}\n")
            f.write(f"Job_ID: {job_id}\n")
            f.write(f"UID: {uid}\n")
            f.write(f"---\n\n")
            f.write(clean_text)

        processed += 1
        print(f"Ingested UID {uid}: {subject[:60]}... ({filename})")

    print(f"Processed {processed} emails.")

    mail.close()
    mail.logout()
    if fetch_failures:
        print(f"[ERROR] {fetch_failures} message(s) failed to fetch.")
        return 1
    print("Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
    