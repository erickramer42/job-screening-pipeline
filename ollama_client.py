#!/usr/bin/env python3
"""
ollama_client.py — the screener's requests to a local Ollama server, and
the repair of what comes back.

A request asks for {"scores": [{"index", "score", "reason"}]} for a
numbered list of listings. The model's answer is untrusted, so:
  - a truncated answer is salvaged only up to its last complete entry;
  - 1-based numbering is detected and shifted, and numbering that could
    be either is refused rather than guessed;
  - every content problem is retried, and the most complete answer kept;
  - a connection failure or a 5xx (TRANSPORT_ERRORS) is told apart from
    a request Ollama rejects (RequestRejected, a 4xx), which no retry
    can fix.

What those failures mean for a run (its exit code) is jobs_screen.py's
decision: this module only raises.
"""
import http.client
import json
import os
import urllib.error
import urllib.request

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("SCREEN_MODEL", "gemma4:12b")

# Retries per request. A truncated, empty or index-shifted response is
# usually fine on a second sample
MAX_ATTEMPTS = 3
# Sampling is seeded, so a repeated request can reproduce. With thinking
# off, the first attempt is greedy; retries, and every request with
# thinking on, sample at the model's default temperature with a seed
# per attempt (greedy decoding would repeat a bad response on retry)
SEED = 42
# Failures of the connection itself (Ollama down, timeout, a response
# cut off mid-body, a 5xx answer). These fail the batch; everything
# wrong with the CONTENT of a response is a ValueError and gets a retry
# instead.
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)

def configure(thinking=False, num_ctx=8192):
    """Set the request settings; jobs_screen.configure() passes them
    from pipeline_config.json (model_thinking, num_ctx)."""
    global MODEL_THINKING, NUM_CTX
    # Model thinking. A thinking model reasons for thousands of tokens
    # before the JSON. Measured 2026-10-22 on the default model:
    #   on:  60-90 s per pass-1 batch; often overflows num_ctx into an
    #        empty or truncated response; at temperature 0 the reasoning
    #        never ends (31k tokens, no answer), so every request must
    #        sample. Keeps the prompt's score caps reliably.
    #   off: ~18 s per batch, no overflows, and the first attempt can be
    #        greedy (temperature 0) for repeatable scores, but the
    #        prompt's caps are broken more often.
    MODEL_THINKING = bool(thinking)
    # The model's context window per request, in tokens: the system
    # prompt, the batch, and the response (and with thinking on, the
    # reasoning, which is what used to overflow it)
    NUM_CTX = num_ctx

configure()

class RequestRejected(Exception):
    """Ollama answered a chat request with a 4xx: the request itself is
    wrong (model_thinking on for a model that can't think, say), so
    every request would be rejected alike and no retry can help."""

def http_error_text(e):
    """An HTTPError with the start of Ollama's error body, which names
    the cause; urllib's own text is only "Bad Request" and the like."""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except OSError:
        body = ""
    try:
        body = json.loads(body).get("error") or body
    except (ValueError, AttributeError):
        pass
    detail = " ".join(str(body).split())[:300]
    return f"HTTP {e.code} {e.reason}" + (f": {detail}" if detail else "")

def sampling_options(attempt):
    if attempt == 1 and not MODEL_THINKING:
        return {"temperature": 0, "seed": SEED}
    return {"seed": SEED + attempt - 1}

# The most context one request has used since the caller last reset it:
# prompt tokens plus response tokens. Ollama's prompt_eval_count counts
# the whole prompt, the system prompt included, even when the prefix was
# cached (checked 2026-10-25). It cuts an over-long prompt without an
# error, so use near num_ctx is the only sign of that
CONTEXT_USED = 0

def ollama_chat(system, user, attempt=1):
    """One chat completion; returns the model's content string.
    Raises TRANSPORT_ERRORS on connection failure or a 5xx answer,
    RequestRejected on a 4xx, and ValueError when the body isn't a chat
    response. Records the request's context use in CONTEXT_USED."""
    global CONTEXT_USED
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": MODEL_THINKING,
        "format": "json",
        "options": {"num_ctx": NUM_CTX, **sampling_options(attempt)},
    }
    req = urllib.request.Request(
        OLLAMA_URL.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.load(resp)  # non-JSON: JSONDecodeError (ValueError)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise RequestRejected(http_error_text(e)) from None
        raise OSError(http_error_text(e)) from None
    content = (data.get("message") or {}).get("content") \
        if isinstance(data, dict) else None
    if not isinstance(content, str):
        raise ValueError("Ollama response has no message content")
    used = sum(n for n in (data.get("prompt_eval_count"),
                           data.get("eval_count")) if isinstance(n, int))
    CONTEXT_USED = max(CONTEXT_USED, used)
    return content

def list_models():
    """Ollama's /api/tags answer as a dict ({} for any other shape).
    Raises TRANSPORT_ERRORS when Ollama can't be reached, and ValueError
    when the answer isn't JSON."""
    with urllib.request.urlopen(OLLAMA_URL.rstrip("/") + "/api/tags",
                                timeout=30) as resp:
        tags = json.load(resp)
    return tags if isinstance(tags, dict) else {}

def model_available(tags, model):
    """True when an /api/tags response lists model. A name without a
    tag means ":latest", as it does in Ollama."""
    names = {m.get("name") for m in (tags or {}).get("models") or []
             if isinstance(m, dict)}
    return model in names or (":" not in model
                              and f"{model}:latest" in names)

def coerce_entry(entry):
    """One model response entry -> (index, score, reason) or None.
    Null/missing/non-numeric fields make the entry unusable. Scores are
    clamped to 0-100; the index is range-checked in align_indices."""
    if not isinstance(entry, dict):
        return None
    try:
        idx = int(entry.get("index"))
        score = int(entry.get("score"))
    except (TypeError, ValueError):
        return None
    reason = str(entry.get("reason") or "").strip()
    return idx, max(0, min(100, score)), reason

def align_indices(rows, batch_size):
    """Map coerced rows to {index: {"score", "reason"}} for 0..n-1.

    Models sometimes number from 1. Index 0 present: 0-based, used as
    is. Index n present without 0: 1-based, shifted down by one (left
    unshifted, every score would land on the neighbouring job). Neither:
    the numbering can't be told apart, so the response is rejected
    (ValueError -> retry) rather than risk misattributed scores.
    """
    idxs = {i for i, _, _ in rows}
    if rows and 0 not in idxs:
        if batch_size not in idxs:
            raise ValueError("indices are neither 0-based nor 1-based")
        rows = [(i - 1, s, r) for i, s, r in rows]
    return {i: {"score": s, "reason": r}
            for i, s, r in rows if 0 <= i < batch_size}

def salvage_scores(text):
    """Recover scores from a response truncated mid-string.

    Cuts at the last '}' (end of the last complete entry) and closes
    the JSON structure. A '}' inside the surviving reason text makes
    the repaired string invalid, which raises out and lets the caller
    retry — so this can never fabricate entries.
    """
    last = text.rfind("}")
    if last == -1:
        raise ValueError("no complete entry in response")
    return json.loads(text[:last + 1] + "]}")

def parse_scores(text, batch_size, attempt):
    """Model content -> {index: {"score", "reason"}}. Raises ValueError
    for anything unusable: empty or unsalvageable text, a body that
    isn't {"scores": [...]}, or indices that can't be aligned."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        if not text.strip():
            # Zero-char / whitespace response: failure mode observed
            # 2026-10-10 (batch 7)
            raise ValueError("empty response from model")
        print(f"    [attempt {attempt}] truncated response "
              f"({len(text)} chars, tail: {text[-60:]!r}) — salvaging")
        parsed = salvage_scores(text)
    # format=json still allows a top-level array or "scores": null
    if not isinstance(parsed, dict) \
            or not isinstance(parsed.get("scores"), list):
        raise ValueError('response is not {"scores": [...]}')
    rows = [r for r in map(coerce_entry, parsed["scores"]) if r]
    return align_indices(rows, batch_size)

def request_scores(system, user, batch):
    """Score one batch with retries. Returns {index: {"score",
    "reason", "attempt"}}: complete as soon as one attempt covers every
    index, otherwise the most complete attempt (possibly {}). attempt is
    the one that answered: only the first is greedy, so a later one's
    score is a sample and needn't repeat. Every problem with a
    response's content is retried. TRANSPORT_ERRORS propagate, unless an
    earlier attempt returned scores: those are returned, like any
    partial result. RequestRejected always propagates."""
    best = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            scores = parse_scores(ollama_chat(system, user, attempt),
                                  len(batch), attempt)
            for s in scores.values():
                s["attempt"] = attempt
        except ValueError as e:
            print(f"    [attempt {attempt}] unusable response ({e}) "
                  f"— retrying")
            continue
        except TRANSPORT_ERRORS as e:
            if not best:
                raise
            print(f"    [attempt {attempt}] request failed ({e}) — keeping "
                  f"{len(best)} scores from an earlier attempt")
            return best
        if len(scores) > len(best):
            best = scores
        missing = len(batch) - len(scores)
        if not missing:
            return scores
        print(f"    [attempt {attempt}] {missing} indices missing "
              f"— retrying")
    return best
