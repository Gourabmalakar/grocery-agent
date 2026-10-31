"""
Paths, caps and mode — the one place every other module reads these from.

Mirrors the family office's engine/config.py pattern: derive ROOT once, create
data directories eagerly, read env through a single tolerant accessor, and
treat everything here as data an agent might reason about rather than a
constant buried in code.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Scripts and cron-style invocations behave identically without every
    # caller having to remember to source .env first.
    pass


def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating shell-style quoting in .env values."""
    return os.getenv(name, default).strip().strip('"').strip("'")


ROOT = Path(__file__).resolve().parent.parent
DATA = Path(_env("DATA_DIR", str(ROOT / "data"))).resolve()
KEYS_DIR = DATA / "keys"
VAULT_DIR = DATA / "vault"
AUDIT_DIR = DATA / "audit"
LEDGER_DB = DATA / "ledger.db"
MANDATES_ISSUED = DATA / "mandates" / "issued"

for _d in (DATA, KEYS_DIR, VAULT_DIR, AUDIT_DIR, MANDATES_ISSUED):
    _d.mkdir(parents=True, exist_ok=True)

# MODE is the single most safety-critical value in the whole project.
# Anything other than the literal string "live" behaves as dry_run: the
# ledger still enforces caps and the mandates still get signed, but the
# rails write to the mock rail instead of touching a real merchant/wallet.
MODE = _env("MODE", "dry_run")
IS_LIVE = MODE == "live"

MOCKSTORE_URL = _env("MOCKSTORE_URL", "http://127.0.0.1:8801")
CONSOLE_URL = _env("CONSOLE_URL", "http://127.0.0.1:8800")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")

# Hard caps, in INR. Enforced in grocery/ledger/budget.py against the SQLite
# ledger before every execute — never in a prompt, never by the model.
CAP_PER_ORDER_INR = int(_env("CAP_PER_ORDER_INR", "800") or "800")
CAP_PER_DAY_INR = int(_env("CAP_PER_DAY_INR", "1500") or "1500")
CAP_PER_MONTH_INR = int(_env("CAP_PER_MONTH_INR", "6000") or "6000")

# Bounded revision rounds (see console/revision.py) — a loop between a
# confused agent and a frustrated human must terminate.
MAX_REVISION_ROUNDS = 5

SESSION_SECRET_KEY = _env("SESSION_SECRET_KEY", "") or "dev-only-insecure-key-set-one-in-.env"

# --------------------------------------------------------------- payments ---
#
#   off      no payment of any kind. The basket is built and that is all.
#   handoff  the basket is built and HE pays in the merchant's own app. No
#            payment authority anywhere in this system. The safe default.
#   cod      handoff PLUS letting the agent PLACE the order for cash on
#            delivery. No instrument is touched and nothing is drainable; the
#            blast radius is one order he can refuse at the door. This is the
#            only fully hands-off path that works without Swiggy enabling
#            anything.
#   wallet   all of the above PLUS paying from the Swiggy Money prepaid float.
#            Needs Swiggy to set agenticPaymentEligible on the account.
#
# EACH LEVEL INCLUDES THE ONES BEFORE IT. The hand-off is never removed, because
# it has to stay available as the fallback whenever a rail refuses.
#
# NOT A LEVEL, AND NEVER WILL BE: UPI. A UPI intent launches the payment app for
# a human to approve, so money sitting in a PhonePe or GPay wallet linked to
# Swiggy does not make an agent able to spend it. The approval IS the point of
# UPI. See grocery/carts/swiggy_checkout.py.
PAYMENT_BACKEND = (_env("PAYMENT_BACKEND", "off") or "off").strip().lower()

# The passphrase that authorises one payment, typed in chat. SEPARATE from
# SESSION_SECRET_KEY so that a Telegram compromise does not also hand over the
# Console, and so it can be rotated on its own.
BOT_PASSPHRASE = _env("BOT_PASSPHRASE", "")

# How long an approval is good for, and how many wrong guesses before the
# system pauses itself. Narrower than the Console's 30-minute live window.
PASSPHRASE_TTL_SECONDS = int(_env("PASSPHRASE_TTL_SECONDS", "300") or "300")
PASSPHRASE_MAX_ATTEMPTS = int(_env("PASSPHRASE_MAX_ATTEMPTS", "3") or "3")


_LEVELS = ("off", "handoff", "cod", "wallet")


def payments_enabled() -> bool:
    return PAYMENT_BACKEND in _LEVELS and PAYMENT_BACKEND != "off"


def _at_least(level: str) -> bool:
    try:
        return _LEVELS.index(PAYMENT_BACKEND) >= _LEVELS.index(level)
    except ValueError:
        return False


def cod_enabled() -> bool:
    """Placing a COD order commits a real purchase, so it needs MODE=live for
    the same reason paying does — the money is owed either way."""
    return _at_least("cod") and IS_LIVE


def autonomous_payment_enabled() -> bool:
    """TWO switches, not one, and they are deliberately different kinds of thing.

    PAYMENT_BACKEND says which rails exist. MODE says whether this process is
    allowed to act on the world at all — it is described in this file as the
    single most safety-critical value in the project, and IS_LIVE was computed
    from it and then checked nowhere in the payment path. So a machine left at
    PAYMENT_BACKEND=wallet would have paid real money while every status page
    it serves says "mode: dry_run".

    The hand-off rail is unaffected: it charges nothing, so it is legitimate in
    dry_run and stays available either way. Only the rail that can actually
    spend requires both.
    """
    return _at_least("wallet") and IS_LIVE


class InsecureConfiguration(RuntimeError):
    """A setting that is merely untidy with payments off becomes a hole with
    them on."""


def assert_safe_to_take_payments() -> None:
    """Called before anything can pay. REFUSES TO START rather than warn.

    SESSION_SECRET_KEY falls back to a public constant checked into this
    repository. With payments off that signs a session cookie for a service
    bound to 127.0.0.1 and is merely sloppy. With payments on, that cookie is
    what stands between a local process and an approval, and its key is printed
    in the source. A default that is only safe while nothing is at stake must
    stop being a default the moment something is.
    """
    if not payments_enabled():
        return
    problems = []
    if SESSION_SECRET_KEY == "dev-only-insecure-key-set-one-in-.env":
        problems.append(
            "SESSION_SECRET_KEY is still the public default from config.py — "
            "set a real one in .env")
    if not BOT_PASSPHRASE:
        problems.append(
            "BOT_PASSPHRASE is unset, so nothing could authorise a payment — "
            "set one in .env")
    if MODE != "live":
        # Not fatal: dry_run with payments configured is a legitimate rehearsal.
        pass
    if problems:
        raise InsecureConfiguration(
            "refusing to enable payments:\n  - " + "\n  - ".join(problems))

# Kill-switch state files. Plain files, deliberately: containment must work
# from a terminal even if the console process is dead.
# THE KILL SWITCH LIVES AT A FIXED, ROOT-RELATIVE PATH ON PURPOSE: `touch
# console/PANIC` has to work from any terminal, by a human in a hurry, without
# knowing where DATA_DIR points. That is the right production default and it is
# kept.
#
# KILLSWITCH_DIR exists for one reason: test isolation. The e2e fixtures spawn
# REAL subprocesses, and a test that trips PANIC through one of them wrote the
# ACTUAL repo file — which halts not just the rest of the suite but any worker
# the human happens to be running at that moment. That is exactly what happened
# on order_f884a496f8d7: a worker mid-revision logged "kill switch is panic —
# stopping immediately" while a test run elsewhere had briefly created the file,
# and the order looked stuck for reasons that had nothing to do with it.
#
# Pointing this at a temp directory in tests makes a suite run unable to stop a
# real order. It is never set in production.
KILLSWITCH_DIR = Path(_env("KILLSWITCH_DIR", str(ROOT / "console")))
PANIC_FILE = KILLSWITCH_DIR / "PANIC"
PAUSE_FILE = KILLSWITCH_DIR / "PAUSE"

# Phase 2 read-only browser adapters (grocery/adapters/*_browser.py) — sites
# like Blinkit gate their catalogue behind a delivery location rather than a
# login. This is a real public coordinate, not a credential; override in
# .env for a different area. Default is Connaught Place, New Delhi.
# FIRST-RUN DEFAULTS ONLY. The delivery address in force is grocery/location.py's
# current(), which the Console edits and stores under DATA — these values seed it
# and are otherwise unused. Do not read them directly from an adapter.
#
# Default is Bengaluru 560001, chosen because it is VERIFIED serviceable: JioMart
# returns twelve Amul products there and zero at the previous 110001 default,
# where quick commerce does not reach. A default that returns an empty catalogue
# looks identical to a broken adapter, which is the worst kind of wrong.
ADAPTER_LOCATION_LAT = float(_env("ADAPTER_LOCATION_LAT", "12.9767") or "12.9767")
ADAPTER_LOCATION_LON = float(_env("ADAPTER_LOCATION_LON", "77.5993") or "77.5993")
ADAPTER_LOCATION_LABEL = _env("ADAPTER_LOCATION_LABEL", "Bengaluru GPO, Bengaluru, Karnataka 560001, India")
# JioMart gates its catalogue on a pincode rather than coordinates (a header,
# not a login — see grocery/adapters/jiomart_api.py). Kept consistent with the
# lat/lon above: 110001 is Connaught Place. Also a public postcode, not a secret.
ADAPTER_LOCATION_PINCODE = _env("ADAPTER_LOCATION_PINCODE", "560001")

# Which adapters the worker searches. "mock" (default) uses mockstore's three
# merchants via HTTP — what every test runs on, network-free. "real" uses
# whichever real adapters are currently reachable (JioMart, Blinkit, Zepto).
MERCHANT_BACKEND = _env("MERCHANT_BACKEND", "mock")

# Merchants to leave out of the comparison entirely, comma-separated. A merchant
# named here is never constructed and never searched — not "tried and skipped".
# Empty by default again as of 2026-10-07. Zepto was disabled here because its
# MCP refused every shopping tool with a "Registration required" error that
# contradicted its own get_user_details (zeptonow/mcp#33). That gate is now
# gone: list_saved_addresses and search_products both answer. Two local fixes
# were needed alongside it — Zepto quotes paise, and select_saved_address drops
# its response stream while still taking effect. See grocery/adapters/zepto_mcp.py.
MERCHANTS_DISABLED = frozenset(
    m.strip().lower() for m in _env("MERCHANTS_DISABLED", "").split(",") if m.strip()
)

# Zepto's MCP gates every shopping tool behind a completed profile and a
# selected delivery address. The profile is finished by a human in Zepto's own
# app — this repo never writes one. The address is READ from Zepto's account
# rather than invented here, and is configurable rather than hardcoded:
#
#   ZEPTO_ADDRESS_ID — which saved address to shop from. Left empty, the
#   adapter proceeds only when the account has exactly ONE saved address;
#   picking the first of several would silently price a neighbourhood nobody
#   chose, which is the mistake blinkit_browser.py refuses to make with lat/lon.
ZEPTO_ADDRESS_ID = _env("ZEPTO_ADDRESS_ID", "")


# ---- the language model ------------------------------------------------
#
# Shopping is not a deterministic task. "remove onion", "250 gm mausambi",
# "a dozen eggs" and "english oven whole wheat bread" are language, and the
# regexes that stood in for understanding bought a potato (order_6a45f91aaa2e).
# A model goes where understanding is actually required — and nowhere else.
#
# OFF BY DEFAULT, deliberately. A bare checkout and the entire test suite must
# never make a network call or need a key, so "off" is what you get unless
# somebody opts in. tests/unit/test_llm_offline.py asserts it.
LLM_BACKEND = _env("LLM_BACKEND", "off")          # "off" | "gemini"
# flash-lite, not flash: it is a separate and far larger free-tier quota, and
# these are structured-extraction tasks rather than hard reasoning. Measured on
# the real list, flash-lite read all eight lines correctly — including
# "mausambi" -> "mosambi", "orhanges" -> "oranges" and "a dozen eggs" -> 12.
# gemini-flash-latest (which resolves to a premium model) returned HTTP 429
# "quota exceeded" after about twenty calls.
LLM_MODEL = _env("LLM_MODEL", "gemini-flash-lite-latest")

# THE KEY IS READ FROM THE ENVIRONMENT AND NEVER FROM CODE, never written to a
# log, a decision record, a narrative or /health, and never placed in a URL —
# it travels as the X-goog-api-key header, because query strings end up in proxy
# logs and crash reports. tests/unit/test_llm_privacy.py asserts it stays out of
# everything this system writes down.
GEMINI_API_KEY = _env("GEMINI_API_KEY", "")

# FREE-TIER GUARDRAILS, enforced locally rather than discovered by being cut
# off. Google does not publish the free-tier numbers on its rate-limit page —
# it says to read them in AI Studio — so these are not a copy of their quota.
# They are our own ceiling, set low enough to sit under any plausible free tier,
# and they fail CLOSED: at the cap the model is simply unavailable and the
# deterministic path runs, with the reason said out loud.
LLM_MAX_CALLS_PER_MINUTE = int(_env("LLM_MAX_CALLS_PER_MINUTE", "12") or "12")
LLM_MAX_CALLS_PER_DAY = int(_env("LLM_MAX_CALLS_PER_DAY", "200") or "200")
LLM_TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "20") or "20")

# Swiggy Instamart's search_products REQUIRES a saved addressId — its schema
# advertises a latitude/longitude "guest mode" that the live server refuses for
# an authenticated token (verified 2026-09-27). Left empty, the adapter uses the
# account's address when there is exactly one and refuses to choose when there
# are several, for the same reason as ZEPTO_ADDRESS_ID above.
SWIGGY_ADDRESS_ID = _env("SWIGGY_ADDRESS_ID", "")


# ---- the chat front door -----------------------------------------------
#
# The bot LONG-POLLS Telegram; it is never a webhook. That is a security
# decision, not a convenience one: the Console is bound to 127.0.0.1 and every
# /api/* route is unauthenticated precisely because only loopback can reach it.
# A webhook would need a public URL and would dissolve that assumption for
# /api/intent, /health and /auth/setup in one move. Polling reaches outward only.
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN", "")

# An allowlist of exactly one chat. Everything else is dropped and audited.
# This is the bot's entire inbound authentication story, and it has to be,
# because this chat is how orders get created.
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")

# The bot's OWN credential for the Console, deliberately not Gourab's passphrase.
#
# The bot needs to revise a cart and approve a cart — both of which change what
# is being bought and therefore need authority, and neither of which moves money.
# Handing it the passphrase would give it everything including payment, forever.
# This token is accepted on exactly three non-money routes (revise, approve_cart,
# choose_basket) and is refused on approve_payment, which keeps needing a human
# passphrase inside a live window. tests/adversarial asserts that boundary.
BOT_API_TOKEN = _env("BOT_API_TOKEN", "")


# ---- which merchants can finish the job --------------------------------
#
# Searching a merchant and being able to BUY from one are different
# capabilities, and conflating them produced a basket Gourab could not act on:
# JioMart won on price and there is no way to put anything in a JioMart cart.
# His instruction: "if you are not able to build real carts pls dont include
# the merchant."
#
# Verified 2026-10-11, not taken from documentation:
#   zepto   — UCP create_cart/update_cart/get_cart/cancel_cart, confirmed by
#             building and cancelling a real cart (cart_f0fb93b5bcb84923b86a)
#   swiggy  — MCP update_cart/get_cart/checkout/confirm_order, surface pinned
#             by --tools; carting not yet exercised end to end
#   jiomart — a pincode-gated HTTP read. No cart, no account, ever.
#   blinkit — a Playwright read that refuses bot-detection rather than evading
#             it. No cart, ever.
#
# The CONSOLE still compares all four — a price you cannot act on is still
# worth knowing, and that is the inspection surface. This list only governs
# what the chat offers as something to approve.
# MERCHANTS SHOWN WITH A BUTTON BUT WHICH CANNOT BUILD A CART. For demos:
# Gourab asked to keep Zepto visible so the three-way comparison is on screen
# while presenting, knowing full well it cannot hand a basket back.
#
# This is a display affordance and nothing more. A merchant listed here is NOT
# in CART_CAPABLE_MERCHANTS, so console/ordering.py still refuses to build for
# it — the demo button routes to an explanation, never to a build. That
# separation is the point: the failure this caused in the first place was a
# merchant being ADVERTISED as buyable while the builder refused it, and the
# human only finding out after approving. Here the button says what it is
# before it is pressed, and pressing it approves nothing and writes nothing.
#
# Set DEMO_MERCHANTS="" to drop it from the chat entirely.
DEMO_MERCHANTS = frozenset(
    m.strip().lower()
    for m in _env("DEMO_MERCHANTS", "zepto").split(",")
    if m.strip()
)

CART_CAPABLE_MERCHANTS = frozenset(
    m.strip().lower()
    # SWIGGY ONLY, as of 2026-10-12. Zepto was dropped not because its cart API
    # failed but because it SUCCEEDED into a void: the basket is real on Zepto's
    # server, scoped to the device that built it (update_cart requires
    # `deviceId`), and its continue_url resolves to the homepage — so the human
    # it was built for cannot reach it from the app or the web. See the comment
    # above _UCP_ENDPOINTS in console/ordering.py for the measurements.
    for m in _env("CART_CAPABLE_MERCHANTS", "swiggy").split(",")
    if m.strip()
)

