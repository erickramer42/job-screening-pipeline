"""
html_text.py — shared text conversion for the pipeline.

Used by job_ingest.py (digest email bodies) and board_fetch.py (posting
descriptions), so both sources get the same rules:
  - Block-level tags become line breaks. Headings, list items and table
    cells stay on their own lines: digest emails lay out title /
    employer / location / View as cells, one field per line, and
    jobs_screen.py finds description headings by line.
  - Entities are decoded only AFTER tags are stripped, so an escaped
    "&lt;" in body text can't turn into a tag and swallow the words
    after it.
  - All whitespace runs, including non-breaking spaces, collapse to a
    single space, so "About the&nbsp;role" matches "about the role".
"""
import html
import re

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>")
_LINE_BREAK = re.compile(
    r"(?i)<br\s*/?>|</(p|div|li|tr|td|th|h[1-6]|table|ul|ol|section)\s*>")
_TAG = re.compile(r"<[^>]+>")

def normalize_text(text):
    """Collapse whitespace within each line and drop blank lines."""
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines())
    return "\n".join(ln for ln in lines if ln)

def html_to_text(markup):
    """Plain text from HTML (or from plain text, which passes through
    with only whitespace normalized)."""
    if not markup:
        return ""
    text = _SCRIPT_STYLE.sub(" ", markup)
    text = _LINE_BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    return normalize_text(html.unescape(text))
