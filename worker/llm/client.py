"""The one place this repo talks to a language model.

IT LIVES IN worker/ AND NOWHERE ELSE. CLAUDE.md keeps models out of grocery/,
and console/ holds the money — so the only process that may call out is the one
that proposes and can never approve. A fully compromised model can mis-rank a
list; it cannot approve a cart, move money, or reach a credential.

FAIL CLOSED, EVERY TIME. No key, backend off, over the rate limit, over the
daily cap, timed out, unparseable JSON — all of them return None, and every
caller treats None as "run the deterministic path and say the model was not
available". A model that silently degrades into a guess is worse than no model,
which is the same rule as an unsourced price (CLAUDE.md rule 3).

THE KEY NEVER APPEARS IN A URL. It goes in the X-goog-api-key header, because
query strings survive in proxy logs, crash reports and shell history. Nothing
here ever logs the key, and _redact() scrubs it from any exception text on the
way out, since a requests error can quote the request it failed on.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

from grocery import config
from worker.llm import privacy

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 2.0

# SELF-PACING. Measured against the real free tier: six calls fired back to back
# got four 429s. Google does not publish the free-tier RPM (its rate-limit page
# says to read it in AI Studio), so rather than guess the number we simply never
# send two requests closer together than this. Being slow is a cost; being rate
# limited mid-order and silently dropping to the deterministic picker is the
# failure this whole change exists to remove.
_MIN_SECONDS_BETWEEN_CALLS = 4.0
_last_call_at = 0.0

# In-process, so a single worker run cannot burst. Deliberately not shared
# across processes: two workers are not a thing this system does, and a
# file-locked bucket would be machinery protecting against nothing.
_recent_calls: list[float] = []


def _usage_path() -> Path:
    return config.DATA / "llm-usage.json"


def _cache_path(key: str) -> Path:
    return config.DATA / "llm-cache" / f"{key}.json"


def available() -> tuple[bool, str]:
    """(usable, why not). The reason is shown to the human, so it is a sentence
    rather than a code — "the model was not consulted" with no explanation is
    the kind of silence this repo treats as a defect."""
    if config.LLM_BACKEND == "off":
        return False, "the language model is switched off (LLM_BACKEND=off)"
    if config.LLM_BACKEND != "gemini":
        return False, f"unknown LLM_BACKEND {config.LLM_BACKEND!r}"
    if not config.GEMINI_API_KEY:
        return False, "no GEMINI_API_KEY is set"
    used = _calls_today()
    if used >= config.LLM_MAX_CALLS_PER_DAY:
        return False, (f"today's self-imposed model budget is spent "
                       f"({used}/{config.LLM_MAX_CALLS_PER_DAY} calls)")
    return True, ""


def _calls_today() -> int:
    try:
        data = json.loads(_usage_path().read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return int(data.get("count", 0)) if data.get("date") == _today() else 0


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _refund_call() -> None:
    """Google answered 503 "high demand" — it never served the request, so it
    should not spend OUR daily budget. Refunding keeps a Google outage from
    quietly exhausting the shopping agent's allowance for the rest of the day.
    A 429 is NOT refunded: that one means the quota really was consumed."""
    try:
        current = _calls_today()
        if current > 0:
            _usage_path().write_text(json.dumps({"date": _today(), "count": current - 1}))
    except OSError:
        pass


def _record_call() -> None:
    """Counted BEFORE the response is known. A request that was sent and then
    timed out still consumed quota at Google's end, so counting on success
    would undercount exactly when things are going wrong."""
    try:
        _usage_path().parent.mkdir(parents=True, exist_ok=True)
        _usage_path().write_text(json.dumps({"date": _today(), "count": _calls_today() + 1}))
    except OSError:
        pass  # usage accounting must never be the thing that breaks a run


def _pace() -> None:
    """Wait, rather than fail, when calls are coming too fast. A short wait
    inside one worker run is invisible to the human; a 429 is a worse cart."""
    global _last_call_at
    gap = time.time() - _last_call_at
    if _last_call_at and gap < _MIN_SECONDS_BETWEEN_CALLS:
        time.sleep(_MIN_SECONDS_BETWEEN_CALLS - gap)
    _last_call_at = time.time()


def _rate_limit_ok() -> bool:
    now = time.time()
    _recent_calls[:] = [t for t in _recent_calls if now - t < 60]
    if len(_recent_calls) >= config.LLM_MAX_CALLS_PER_MINUTE:
        return False
    _recent_calls.append(now)
    return True


def _redact(text: str) -> str:
    key = config.GEMINI_API_KEY
    return text.replace(key, "<GEMINI_API_KEY>") if key else text


def ask_json(prompt: str, *, schema: dict[str, Any], purpose: str,
             max_output_tokens: int = 2048) -> Any | None:
    """One structured question. Returns parsed JSON, or None for ANY failure.

    `purpose` is a short slug recorded with usage so a surprising bill (or a
    surprising silence) can be traced to the step that caused it. It is never
    sent to Google.

    The response is pinned with responseMimeType=application/json AND a
    responseSchema, so the model returns a shape rather than prose we then have
    to guess at. The caller still validates — a schema the server enforces is
    not the same as a claim we have checked, and every caller here re-checks
    indices and values against the data it sent.
    """
    usable, _why = available()
    if not usable:
        return None

    # The guard runs before anything is sent, every time, for every caller — and
    # a refusal stops the CALL, not the order. It fired on a legitimate tomato
    # price once and crashed the worker mid-basket (order_f884a496f8d7), which
    # turned a prompt-hygiene check into an outage. Nothing is sent either way;
    # the difference is whether the human still gets a cart.
    try:
        privacy.assert_safe(prompt)
    except privacy.PromptLeak as exc:
        # Loud, because this is a bug in whoever built the prompt and must not
        # decay into background noise. The message never carries the value.
        print(f"[llm] {purpose}: PROMPT REFUSED — {exc}. Nothing was sent; "
              f"continuing without the model.")
        return None

    cache_key = hashlib.sha256(
        f"{config.LLM_MODEL}\x00{prompt}\x00{json.dumps(schema, sort_keys=True)}".encode()
    ).hexdigest()
    cached = _read_cache(cache_key)
    if cached is not None:
        return cached

    if not _rate_limit_ok():
        return None
    _pace()

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # Zero temperature because the same basket must produce the same
            # cart twice — the eval suite's `consistency` criterion grades
            # exactly that, and a shopping agent that wanders is not auditable.
            "temperature": 0,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    # A SHORT, POLITE RETRY — and only for the two codes that mean "ask again",
    # never for a 4xx that means "this request is wrong". The free tier answers
    # 503 "high demand" often enough that giving up on the first one would make
    # the model effectively unavailable, and retrying a bad request instead of
    # fixing it is how you get rate limited for nothing.
    response = None
    for attempt in range(_MAX_ATTEMPTS):
        _record_call()
        try:
            response = requests.post(
                _ENDPOINT.format(model=config.LLM_MODEL),
                headers={"Content-Type": "application/json",
                         "X-goog-api-key": config.GEMINI_API_KEY},
                json=body,
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            print(f"[llm] {purpose}: unreachable ({_redact(str(exc))[:160]})")
            return None

        if response.status_code == 200:
            break
        if response.status_code == 503 and attempt < _MAX_ATTEMPTS - 1:
            _refund_call()
            time.sleep(_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.status_code == 503:
            _refund_call()
            print(f"[llm] {purpose}: Gemini is overloaded — using the deterministic path")
            return None
        if response.status_code == 429:
            print(f"[llm] {purpose}: rate limited by Google — using the deterministic path")
            return None
        print(f"[llm] {purpose}: HTTP {response.status_code} — {_redact(response.text)[:160]}")
        return None

    if response is None or response.status_code != 200:
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except (ValueError, KeyError, IndexError) as exc:
        print(f"[llm] {purpose}: unusable response ({_redact(str(exc))[:120]})")
        return None

    _write_cache(cache_key, parsed)
    return parsed


def _read_cache(key: str) -> Any | None:
    """A cache hit costs nothing and counts against no quota — which is most of
    how this stays inside a free tier. Keyed on model+prompt+schema, so changing
    any of them is a different question and correctly misses."""
    try:
        return json.loads(_cache_path(key).read_text())["response"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _write_cache(key: str, value: Any) -> None:
    try:
        path = _cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"response": value, "at": time.time()}))
        path.chmod(0o600)
    except OSError:
        pass


def usage_summary() -> dict[str, Any]:
    """Safe for a status page: counts and limits, never the key."""
    usable, why = available()
    return {"backend": config.LLM_BACKEND, "model": config.LLM_MODEL,
            "key_present": bool(config.GEMINI_API_KEY),
            "calls_today": _calls_today(), "daily_cap": config.LLM_MAX_CALLS_PER_DAY,
            "usable": usable, "unavailable_because": why}
