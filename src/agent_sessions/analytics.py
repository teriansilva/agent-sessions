"""Opt-in usage analytics (#1009): one pseudonymous active-install report a day, to Umami.

What it answers is narrow on purpose — how many consenting installs had an operator open the app on
a given day — so what it sends is narrow too: a random install id, the app version and the OS name,
as one Umami pageview. Umami keys a visitor on ``uuid(website, id)`` when the payload carries an
``id``, so one install is one visitor however its IP moves; and its Visitors figure excludes custom
events, which is why this is a pageview rather than a named event.

**Consent is the only switch that matters.** Undecided is off, and nothing here ever records a
decision — ``prefs.set_analytics_consent`` is called by the route, from the operator's own save.
``AGENT_SESSIONS_ANALYTICS=0`` turns the whole module off for a server regardless of the stored
decision (and is what the test suite runs under).

**The trigger is the authenticated config fetch** (``note_active``), not a timer: a daemon left
running on a forgotten box is not an active user.

**Delivery is best effort, bounded per install per UTC day**: at most one settled report and at most
``ANALYTICS_MAX_ATTEMPTS`` requests. The attempt is claimed durably *before* a byte is sent, so no
crash, restart or consent toggle can exceed the cap; retries within a process are spaced an hour
apart. A switch-off is not coordinated with dispatch by a lock — holding one across a five-second
POST would stall the prefs route — so the residual is disclosed instead of hidden: an attempt whose
pre-dispatch re-check already passed still goes out, and one on the wire is not recalled. Neither
can write the day's settle back, and neither can bring a deleted id back.

The destination is fixed. Nothing from a request, a pref or model output reaches the URL, the
headers or the body. It is an inventoried outbound call (``tests/test_prompts_registry.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import threading
import time

import httpx

from . import prefs
from .version import get_version

log = logging.getLogger("agent_sessions.analytics")

#: The project's self-hosted Umami, and the website that collects app reports (distinct from the
#: landing page's, so neither pollutes the other's figures).
UMAMI_SEND_URL = "https://analytics.superstatus.io/api/send"
WEBSITE_ID = "5ba4937b-3b67-487a-bb15-00914501b371"
#: Umami requires a hostname and derives the page from the URL; neither names a real host.
REPORT_HOSTNAME = "battlelab-app"
REPORT_TITLE = "BattleLab"

TIMEOUT_S = 5.0
RETRY_SPACING_S = 3600.0

#: Test seam, like ``webpush._TRANSPORT``: a ``MockTransport`` so no test reaches the network.
_TRANSPORT: httpx.BaseTransport | None = None

_lock = threading.Lock()
_in_flight = False
_last_attempt: float | None = None
_tasks: set[asyncio.Future] = set()


def available() -> bool:
    """False when the server has turned analytics off wholesale (``AGENT_SESSIONS_ANALYTICS=0``)."""
    return (os.environ.get("AGENT_SESSIONS_ANALYTICS", "1") or "1") != "0"


def public_state() -> dict:
    """What ``/api/config`` and the prefs route return: never the id, never the budget."""
    return {**prefs.get_analytics(), "available": available()}


def set_consent(value: bool) -> dict:
    """The operator's decision, recorded. Returns the public state."""
    prefs.set_analytics_consent(value)
    return public_state()


def os_name() -> str:
    return platform.system() or "unknown"


def user_agent(version: str, system: str) -> str:
    """``BattleLab/<version> (<OS>)``. The shape is load-bearing: Umami's bot filter silently
    discards ``BattleLab/<version>`` and httpx's default UA (probed live on v3.2.0), answering
    ``200 {"beep":"boop"}`` and recording nothing."""
    return f"BattleLab/{version} ({system})"


def build_payload(install_id: str, version: str, system: str) -> dict:
    """The whole body. Deliberately no ``ip``, ``data``, ``screen``, ``language`` or ``referrer`` —
    and ``ip`` in particular would also override the instance's LAN exclusion."""
    return {
        "type": "event",
        "payload": {
            "website": WEBSITE_ID,
            "id": install_id,
            "hostname": REPORT_HOSTNAME,
            "url": f"/{version}",
            "title": REPORT_TITLE,
            "os": system,
        },
    }


def is_due(block: dict, today: str) -> bool:
    """Consent on, today not settled, and today's attempts not spent."""
    if block.get("consent") is not True or not block.get("install_id"):
        return False
    if block.get("last_sent_day") == today:
        return False
    spent = block.get("attempts", 0) if block.get("attempt_day") == today else 0
    return spent < prefs.ANALYTICS_MAX_ATTEMPTS


def note_active() -> None:
    """The operator fetched the app config. Starts today's report if one is due. Called on the event
    loop from the config route: cheap, never raises, never awaited by the response."""
    global _in_flight
    try:
        if not available() or not is_due(prefs.analytics_state(), prefs.utc_today()):
            return
        loop = asyncio.get_running_loop()
        with _lock:
            if _in_flight:
                return
            if _last_attempt is not None and time.monotonic() - _last_attempt < RETRY_SPACING_S:
                return
            _in_flight = True
        # The claim belongs to the WORKER, not the coroutine (#1153, Hermes 5292): `_run` marks
        # this token before it does anything, and releases the claim in its own `finally`. If it
        # never starts — the task cancelled before its first step, or the executor future cancelled
        # while still queued behind busy workers — `settle` releases it instead, and marks the token
        # so a worker that picks the job up after all does nothing.
        token = _Dispatch()
        task = loop.create_task(asyncio.to_thread(_run, token))
        _tasks.add(task)
        task.add_done_callback(lambda t: _settle(t, token))
    except Exception:  # noqa: BLE001 — analytics must never break the config route
        log.exception("usage analytics: could not start a report")


class _Dispatch:
    """One scheduled report: whether its worker ran, or was abandoned before it could."""

    __slots__ = ("abandoned", "ran")

    def __init__(self) -> None:
        self.ran = False
        self.abandoned = False


def _settle(task: asyncio.Future, token: _Dispatch) -> None:
    """Done callback of the dispatch task: release the claim iff its worker never started."""
    global _in_flight
    _tasks.discard(task)
    with _lock:
        if not token.ran:
            # Nothing else will release it: left set, every later `note_active` would return
            # early and the day's report would never be sent.
            token.abandoned = True
            _in_flight = False


def _run(token: _Dispatch | None = None) -> None:
    global _in_flight
    if token is not None:
        with _lock:
            if token.abandoned:
                return  # its claim was already released; a newer one may exist — leave it alone
            token.ran = True
    try:
        send_once()
    except Exception:  # noqa: BLE001
        log.exception("usage analytics: report crashed")
    finally:
        with _lock:
            _in_flight = False


def _after_claim() -> None:
    """Test seam: runs between the durable claim and the pre-dispatch re-check."""


def send_once() -> str:
    """Claim → re-check → POST → settle. **Blocking** — run it in a thread. Returns the outcome:
    ``not-due``, ``withdrawn``, ``recorded``, ``bot-filtered``, ``refused`` or ``transient``."""
    global _last_attempt
    today = prefs.utc_today()
    claimed: dict = {}

    def claim(block: dict) -> dict | None:
        if not is_due(block, today):
            return None
        spent = block.get("attempts", 0) if block.get("attempt_day") == today else 0
        block["attempt_day"] = today
        block["attempts"] = spent + 1
        claimed["id"] = block["install_id"]
        return block

    prefs.update_analytics(claim)
    if "id" not in claimed:
        return "not-due"
    with _lock:
        _last_attempt = time.monotonic()
    _after_claim()

    # Pre-dispatch re-check. The attempt stays spent either way: the budget only ever errs low.
    current = prefs.analytics_state()
    if current.get("consent") is not True or current.get("install_id") != claimed["id"]:
        return "withdrawn"

    version = get_version()
    system = os_name()
    client_kwargs: dict = {"timeout": TIMEOUT_S, "trust_env": False, "follow_redirects": False}
    if _TRANSPORT is not None:
        client_kwargs["transport"] = _TRANSPORT
    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(
                UMAMI_SEND_URL,
                json=build_payload(claimed["id"], version, system),
                headers={"User-Agent": user_agent(version, system)},
            )
    except httpx.HTTPError as e:
        log.info("usage analytics: report not delivered (%s)", e.__class__.__name__)
        return "transient"

    outcome = _classify(resp)
    if outcome == "transient":
        log.info("usage analytics: report not delivered (HTTP %s)", resp.status_code)
        return outcome
    if outcome == "bot-filtered":
        log.warning("usage analytics: report discarded by the bot filter — check the User-Agent")
    elif outcome == "refused":
        log.info("usage analytics: report refused (HTTP %s)", resp.status_code)

    def settle(block: dict) -> dict | None:
        # Only for the consent and the id this attempt was claimed under: a switch-off (or an
        # off → on, which mints a new id) that landed during the POST must not be written over.
        if block.get("consent") is not True or block.get("install_id") != claimed["id"]:
            return None
        block["last_sent_day"] = today
        return block

    prefs.update_analytics(settle)
    return outcome


def _classify(resp: httpx.Response) -> str:
    """Every answer except a server error settles the day: retrying a refusal cannot help."""
    if resp.status_code >= 500:
        return "transient"
    if not 200 <= resp.status_code < 300:
        return "refused"
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("beep") == "boop":
        return "bot-filtered"
    return "recorded"
