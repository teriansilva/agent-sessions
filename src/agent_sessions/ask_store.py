"""Ask's durable conversations (#1443 Phase 1).

The Ask sidebar's conversation used to live in the browser only. An orchestrator whose answers,
proposals and follow-up updates vanish on a reload cannot keep an audit trail, so each
conversation is now an append-only JSONL file on the server:

    <root>/<cid>.jsonl            the records, one JSON object per LF-terminated line
    <root>/proposals/<cid>.json   Phase 3's session proposals (``ask_proposals`` writes them)
    <root>/follows/<cid>.json     Phase 4's follows (``ask_follow`` writes them, through here)
    <root>/.locks/<cid>.lock      the turn lock: held for as long as a turn runs
    <root>/.locks/<cid>.proposals.lock   held across every read-modify-write of the sidecar
    <root>/.locks/<cid>.decide.lock      held for as long as a launch from this conversation runs
    <root>/.locks/<cid>.follows.lock     held across every read-modify-write of the follows

``root`` is ``AGENT_SESSIONS_ASK_DIR`` (default ``~/.config/agent-sessions/ask``), kept 0700 with
0600 files. Its parent is the app's config dir (often 0775 by umask), so the chain is not held to
``privatedir``'s rule — the chat store's and the memory store's stance.

**The store boundary.** :func:`append` is the only writer, and every record passes it:

* Free text — the operator's messages, the answers, match reasons and titles, failure details and
  tool arguments — first has every stored or recently typed template secret redacted (the
  redactor ``review._post_chat`` applies; again here because an answer can quote what it was
  shown), and then, if what is left shows ANY credential trigger (``template_suggest``'s
  ``credential_shaped``), the whole field is replaced by :data:`WITHHELD`. Withheld, never
  partially redacted (``template-suggest.md``: redacting needs to know where a value ends).
  Redaction that cannot run refuses the write (:class:`StoreRefused`): nothing unscrubbed lands.
* Structural fields (ids, keys, states, counts) are shape-checked; a record that fails is a bug
  and raises rather than being repaired.
* **Raw tool output never reaches this module.** A ``tool`` record carries the tool's name, its
  validated arguments and result METADATA (counts, the keys and mission ids returned, a length, an
  error code). Screen tails and transcript excerpts exist only in the running turn's memory.

**Retention** (:func:`sweep`): 30 days since a conversation last changed, and at most 50
conversations. A conversation's JSONL, proposal sidecar, follow sidecar and lock file go together.
A conversation is PINNED — kept past both limits — while a turn is running in it or while its
proposal sidecar holds a ``deciding`` / ``creating`` proposal (or cannot be read): retention never
deletes a record recovery still needs to tell whether an effect happened. Admitted HQ memories
also retain their source conversation through routine expiry. Explicit conversation deletion
still removes its derived memories; forgetting the last note releases that retention pin.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from . import ask_screen, hq_contacts, missions, template_secrets, template_suggest

log = logging.getLogger(__name__)

CID_RE = re.compile(r"^ask_[0-9a-f]{32}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
#: A session key's SHAPE (`<engine>:<native id>`). The roster is not consulted: a key whose engine
#: was removed is still a fact about what an answer named.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}:[A-Za-z0-9._:@-]{1,200}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TOOL_NAME_RE = re.compile(r"^[a-z_]{1,40}$")
_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: What a field that looked like a credential is stored as.
WITHHELD = "[withheld: looked like a credential]"

RETENTION_DAYS = 30
MAX_CONVERSATIONS = 50
#: Turns one conversation may hold; past it the operator starts a new one (honest, not a cut).
MAX_TURNS = 50
#: The most of one conversation file read back. Above the worst case by construction: a turn is
#: at most a question and an answer (TEXT_MAX each), 8+8 matches, three status records (a turn
#: that waited in the queue has `queued`, `pending` and its outcome, #2103), its first
#: `ask_orchestrator.STEP_RECORDS_MAX` no-effect tool records (ARG_MAX of arguments and
#: META_IDS_MAX ids each), one record per step that had or may have had an effect (bounded by the
#: send and memory caps and by `MAX_SESSION_PROPOSALS` cards a conversation), and one `steps_more`
#: summary. A turn's WORK is not bounded (#2076); the detail kept of it is. The worst case is
#: measured, not estimated, by `test_the_worst_case_conversation_fits_the_read_cap`.
READ_MAX_BYTES = 96 * 1024 * 1024
#: The head read for the conversation list: enough for the header and the first question.
_HEAD_BYTES = 64 * 1024

# Per-field caps, applied AFTER redaction and the credential check.
TEXT_MAX = 4_000
TITLE_MAX = 120
WHY_MAX = 200
DETAIL_MAX = 400
ARG_MAX = 300
#: Arguments kept per tool record — no read tool takes more than two.
ARGS_MAX_ITEMS = 2
LIST_TITLE_MAX = 80
MATCHES_MAX = 8
META_IDS_MAX = 20

#: Proposal states that pin a conversation past retention (Phase 3 writes them).
PINNING_PROPOSAL_STATES = frozenset({"deciding", "creating"})

#: `stopped` (#2076): the Commander ended the turn before HQ answered; its records are kept.
TURN_STATUSES = frozenset(
    {"pending", "done", "failed", "stopped", "queued", "withdrawn", "not_started"}
)
#: A queued turn's life before it runs, and the two ways it ends without running (#2103,
#: `hq-tasks.md`): ``queued`` waits behind the running turn; ``withdrawn`` was taken back by the
#: Commander (Withdraw, Stop all); ``not_started`` was still queued when the server stopped.
UNSTARTED_STATUSES = frozenset({"queued", "withdrawn", "not_started"})
#: What a `send` step's record says of its message: delivered, not sent, or not known.
SENT_OUTCOMES = frozenset({"yes", "no", "unknown"})
#: The largest number a step record or a `steps_more` summary keeps (an ordinal, a duration in
#: milliseconds, a count): bounds the bytes a number can take.
STEP_NUMBER_MAX = 10**12
#: Tool names one `steps_more` summary may count, each way (run, refused).
STEPS_MORE_NAMES_MAX = 128
#: How a question may arrive besides typing (#1397). `call` = spoken on an in-app call; the
#: phone and WhatsApp channels (#1397 P5) add theirs here when they exist. `card` = not a question
#: at all: the server's continuation after a confirmed draft-mission card (#1950,
#: `ask_orchestrator.continue_after_card`), its text server-written.
VIAS = frozenset({"call", "card"})
#: A user record's step-list key (#2251, `ask_steps.owed`): what settled, and where.
STEPS_KEY_RE = re.compile(r"^(next|hold|done):[0-9a-f]{16}:s[0-9a-f]{8}:[0-9a-f]{16}$")
STEPS_SEEN_MAX = 8
FIND_SOURCES = frozenset({"index", "index+model", "model"})
#: The card states a turn's model request carried (#2372), kept on its final status so the next
#: turn knows which settlements HQ has already been told about. Bounded; the newest win.
CARDS_SEEN_MAX = 64
_CARD_STATE_RE = re.compile(r"^[a-z_]{1,20}$")

RECORD_TYPES = frozenset(
    {
        "conversation", "user", "assistant", "tool", "status", "question", "proposal", "update",
        "steps_more", "screen", "interjection", "heard", "heard_link", "said", "counts",
        "summary",
    }
)  # fmt: skip

# A call's own record of what it heard and said (#2137). `heard`: one per utterance the speech
# worker delivered, written as it arrives, before anything routes it. A DROPPED utterance (HQ's
# own echo, a language the call does not listen in, a stock no-speech phrase) keeps NO words: only
# why, its heard language and its length, so a bystander's speech is never stored. `heard_link`:
# appended once the call knows where those words went (the store is append-only, never rewritten):
# the route, and the HQ turn they became part of. `said`: what HQ spoke outside an answer (the
# greeting, a card read-back, a settle outcome, the still-there question). `counts`: how many of
# each a call could not store past the bounds below.
HEARD_ROUTES = frozenset(
    {
        "hq",
        "held",
        "queued",
        "joined",
        "interject",
        "confirm",
        "stop",
        "muted",
        "dropped",
        "goodbye",
    }
)
HEARD_DROPS = frozenset({"echo", "foreign", "no_speech"})
SAID_KINDS = frozenset({"greeting", "readback", "outcome", "still_there", "farewell"})
#: Bounds per conversation (#2137): past them a call counts instead of storing.
HEARD_DROPPED_MAX = 200
HEARD_SAID_MAX = 2_000
#: `counts` records: at most one per call, and only past a bound; capped too, since calls are not.
HEARD_COUNTS_MAX = 50
#: A background summary of a conversation's oldest exchanges (#2137 P2): its own guarded field
#: cap (a summary over it is REFUSED by the summariser, never truncated), and how many one
#: conversation may hold. Each new one covers everything its predecessor did, so only the newest
#: is replayed.
SUMMARY_TEXT_MAX = 6_000
SUMMARIES_MAX = 3
#: A turn's context size (#2137 P2): tokens of its latest answer request, measured or estimated,
#: and the model's window when known. Numbers only.
CTX_NUMBER_MAX = 10**8
_HEARD_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_LANG_RE = re.compile(r"^[a-z]{2}$")
#: The worker's turn numbers, and an utterance's length in characters, are bounded like any number.
HEARD_NUMBER_MAX = 10**9

# A shared file's descriptor on a `user` record (#1707): the blob lives beside the conversation
# (`ask_attachments`), this is only its metadata and is shape-checked here like every structural
# field. The name is stored text (guard_text); the id is a server-made UUID hex; the kind mirrors
# `ask_attachments.KINDS` and is decided from the bytes at publish, never taken on a client's word.
ATTACHMENT_KINDS = frozenset({"image", "text", "other"})
ATTACHMENTS_MAX = 4
_AID_RE = re.compile(r"^[0-9a-f]{32}$")

# Asking back (#1443 Phase 2): a question card's bounds.
QUESTION_MAX = 300
OPTION_MAX = 120
OPTIONS_MAX = 4
#: A nudge's text — the orchestrator's own `answer` cap, so the ledger never cuts what was shown.
NUDGE_MAX = 800
#: A session proposal's brief (#1443 Phase 3a), well under the seed cap so the attempt's own
#: identifying line always fits beside it.
BRIEF_MAX = 2_000
_ACTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
#: An HQ operation (#1559): a catalog id, its risk class, and the caps of what the card shows.
_OP_RE = re.compile(r"^[a-z]{1,20}\.[a-z_]{1,40}$")
OPERATION_RISKS = frozenset({"reversible", "destructive", "external"})
OPERATION_STATES = frozenset(
    {
        "pending",
        "deciding",
        "done",
        "partly_done",
        "failed",
        "stale",
        "dismissed",
        "interrupted",
        "unconfirmed",
        "unsettled",
        # Nothing changed and nothing can run: the route's store refused it (#1938), or a skill
        # import the Library can't store was staged settled (#2145).
        "refused",
    }
)
OP_VERB_MAX = 40
#: What an operation card may ask the OPERATOR for (never the model): a folder they pick.
OPERATION_INPUTS = frozenset({"folder", "value", "file"})
OP_SUMMARY_MAX = 400
_OP_FIELD_RE = re.compile(r"^[a-z_]{1,40}$")
OP_FIELDS_MAX = 8
PROPOSAL_KINDS = frozenset({"nudge", "session", "operation", "message"})
#: A message HQ proposes through a messenger connector (#1833): its text is the EXACT bytes a tap
#: sends, so it is bounded by the largest provider limit and never re-rendered.
MESSAGE_MAX = 32_000
MESSAGE_STATES = frozenset(
    {"pending", "deciding", "sent", "failed", "unconfirmed", "stale", "dismissed", "interrupted"}
)
_CONNECTOR_ID_RE = re.compile(r"^c_[0-9a-f]{12}$")
_RECIPIENT_ID_RE = re.compile(r"^[!A-Za-z0-9:._=+/-]{1,255}$")
_RECIPIENT_KINDS = frozenset({"private chat", "group", "supergroup", "channel", "room"})
# Follows (#1443 Phase 4): what a followed session's update says, as a kind — the words are the
# client's, authored, so no model text is ever shown as an update.
UPDATE_KINDS = frozenset({"finished", "needs_you", "ended", "not_followed"})
#: Why a conversation follows a session: Ask STARTED it, or a nudge Ask proposed was DELIVERED to it
#: (#1517).
FOLLOW_VIAS = frozenset({"start", "nudge"})
#: Sessions one conversation follows at once, and entries kept (an unfollowed one is remembered so
#: the same delivered nudge never re-follows it).
MAX_FOLLOWS = 10
MAX_FOLLOW_ROWS = 50
#: Update records one conversation may hold; past it its follows end, so the size bound holds.
MAX_UPDATES = 300
#: Ids a conversation has followed FOR (a started proposal) — never
#: forgotten, so a replayed reason never re-follows after Unfollow. Bounded by construction: a
#: conversation holds at most `MAX_SESSION_PROPOSALS` proposals of either kind.
MAX_CONSUMED = MAX_TURNS * 3


class StoreRefused(Exception):
    """A write the store boundary will not make (redaction unavailable)."""


class ConversationNotFound(Exception):
    pass


def root() -> Path:
    raw = os.environ.get("AGENT_SESSIONS_ASK_DIR")
    return Path(raw) if raw else Path.home() / ".config" / "agent-sessions" / "ask"


def valid_cid(value: object) -> bool:
    return isinstance(value, str) and CID_RE.fullmatch(value) is not None


def valid_turn_id(value: object) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def new_cid() -> str:
    return "ask_" + uuid.uuid4().hex


def _path(cid: str) -> Path:
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    return root() / f"{cid}.jsonl"


def _proposals_path(cid: str) -> Path:
    return root() / "proposals" / f"{cid}.json"


def _follows_path(cid: str) -> Path:
    return root() / "follows" / f"{cid}.json"


def _steps_path(cid: str) -> Path:
    return root() / "steps" / f"{cid}.json"


def _lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.lock"


def _proposals_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.proposals.lock"


def _steps_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.steps.lock"


def _follows_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.follows.lock"


def _decide_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.decide.lock"


def _ensure_dir(path: Path) -> None:
    """A real directory of this user, kept 0700."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise StoreRefused(f"{path.name}: not a directory of this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _ensure_root() -> None:
    _ensure_dir(root())
    _ensure_dir(root() / ".locks")


# ---- the store boundary ------------------------------------------------------------------------


def _mask_ids(text: str) -> str:
    """Mission ids are identifiers, not secrets: a 32-hex run behind ``msn_`` would otherwise read
    as a random-looking credential and withhold an ordinary answer that names a mission."""
    return re.sub(r"msn_[0-9a-f]{32}", "msn_id", text)


def guard_text(value: object, cap: int, *, secrets: list[str] | None = None) -> str:
    """One persisted text field, through the boundary: redact stored/typed secrets, withhold the
    whole field if it still looks like a credential, then cap."""
    if not isinstance(value, str):
        return ""
    try:
        text = template_secrets.redact_text(value, secrets)
    except template_secrets.RedactionUnavailable as e:
        raise StoreRefused(f"{e} — nothing was stored") from None
    if template_suggest.credential_shaped(_mask_ids(text)):
        return WITHHELD
    return text[:cap]


def question_digest(text: str) -> str:
    """What binds a ``turn_id`` to the question it was sent with: an HMAC of the ORIGINAL text
    under the template-secret store's key — never the stored text, which redaction and the withhold
    rule can make identical for two different questions. Fails closed (StoreRefused)."""
    try:
        return template_secrets.keyed_digest("ask-question", text)
    except template_secrets.SecretKeyUnavailable as e:
        raise StoreRefused(f"{e} — nothing was stored") from None


def _turn_id(rec: dict) -> str:
    tid = rec.get("turn_id")
    if not valid_turn_id(tid):
        raise ValueError("malformed turn id")
    return tid  # type: ignore[return-value]


def _key_list(values: object, *, missions_: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values[:META_IDS_MAX]:
        ok = (
            isinstance(v, str) and missions.MISSION_ID_RE.match(v) is not None
            if missions_
            else isinstance(v, str) and _KEY_RE.match(v) is not None
        )
        if not ok:
            raise ValueError("malformed id in tool metadata")
        out.append(v)
    return out


def _admit(rec: dict, secrets: list[str]) -> dict:
    """The record as it may be written, or ValueError for a record no caller should produce."""
    kind = rec.get("type")
    if kind not in RECORD_TYPES:
        raise ValueError(f"unknown record type {kind!r}")
    ts = rec.get("ts")
    out: dict = {"type": kind, "ts": float(ts) if isinstance(ts, int | float) else time.time()}

    def text(value: object, cap: int) -> str:
        return guard_text(value, cap, secrets=secrets)

    if kind == "conversation":
        cid = rec.get("id")
        if not valid_cid(cid):
            raise ValueError("malformed conversation id")
        out["id"] = cid
    elif kind == "user":
        out["turn_id"] = _turn_id(rec)
        out["text"] = text(rec.get("text"), TEXT_MAX)
        qd = rec.get("qd")
        if not (isinstance(qd, str) and _DIGEST_RE.match(qd)):
            raise ValueError("a question needs its digest")
        out["qd"] = qd
        answers = rec.get("answers")
        if answers is not None:
            if not valid_turn_id(answers):
                raise ValueError("malformed question id")
            out["answers"] = answers
        # Who answers this turn (#1525): a known HQ contact id or "off", resolved by the route.
        # Stored on the turn so an earlier answer keeps its speaker after the operator switches.
        contact = rec.get("contact")
        if contact is not None:
            if not isinstance(contact, str) or contact not in (*hq_contacts.IDS, hq_contacts.OFF):
                raise ValueError("unknown HQ contact")
            out["contact"] = contact
        # How the question arrived (#1397): absent = typed; a call turn says so. Only known
        # channels are kept — a record carrying anything else is refused, never repaired.
        via = rec.get("via")
        if via is not None:
            if via not in VIAS:
                raise ValueError("unknown channel")
            out["via"] = via
        # The step-list settlement this turn takes up (#2251): the server's own key, written in
        # the same append as the turn, so an advance or hold is begun exactly once.
        steps_key = rec.get("steps_key")
        if steps_key is not None:
            if not (isinstance(steps_key, str) and STEPS_KEY_RE.match(steps_key)):
                raise ValueError("malformed steps key")
            out["steps_key"] = steps_key
        # The draft a Plan with HQ bound this turn to (#2152): a mission id, validated by the
        # route. Anything else is refused, never repaired.
        mission = rec.get("mission")
        if mission is not None:
            if not (isinstance(mission, str) and missions.MISSION_ID_RE.match(mission)):
                raise ValueError("malformed mission id")
            out["mission"] = mission
        # Files shared with this question (#1707): descriptors only — the bytes never enter the
        # JSONL. An id is a server-made UUID hex, the name stored text, the kind one of three.
        atts = rec.get("attachments")
        if atts is not None:
            if not isinstance(atts, list) or len(atts) > ATTACHMENTS_MAX:
                raise ValueError("malformed attachments")
            admitted: list[dict] = []
            for a in atts:
                if not isinstance(a, dict):
                    raise ValueError("malformed attachment")
                aid = a.get("id")
                if not (isinstance(aid, str) and _AID_RE.match(aid)):
                    raise ValueError("malformed attachment id")
                kind = a.get("kind")
                if kind not in ATTACHMENT_KINDS:
                    raise ValueError("unknown attachment kind")
                size = a.get("size")
                if not (
                    isinstance(size, int)
                    and not isinstance(size, bool)
                    and 0 < size <= 10 * 1024 * 1024
                ):
                    raise ValueError("malformed attachment size")
                if "secret" in a and type(a["secret"]) is not bool:
                    raise ValueError("malformed attachment secret flag")
                admitted.append(
                    {
                        "id": aid,
                        "name": text(a.get("name"), 200),
                        "kind": kind,
                        "size": size,
                        # #1786: shared as a secret — its bytes never reach a model.
                        **({"secret": True} if a.get("secret") is True else {}),
                    }
                )
            out["attachments"] = admitted
    elif kind == "question":
        out["turn_id"] = _turn_id(rec)
        qid = rec.get("question_id")
        if not valid_turn_id(qid):
            raise ValueError("malformed question id")
        out["question_id"] = qid
        out["question"] = text(rec.get("question"), QUESTION_MAX)
        options = rec.get("options") or []
        if not isinstance(options, list) or len(options) > OPTIONS_MAX:
            raise ValueError("malformed question options")
        out["options"] = [text(o, OPTION_MAX) for o in options]
        out["allow_text"] = rec.get("allow_text") is True
    elif kind == "proposal":
        out["turn_id"] = _turn_id(rec)
        aid, key, pkind = rec.get("action_id"), rec.get("key"), rec.get("kind")
        if not (isinstance(aid, str) and _ACTION_ID_RE.match(aid)):
            raise ValueError("malformed action id")
        if pkind not in PROPOSAL_KINDS:
            raise ValueError("unknown proposal kind")
        if pkind == "nudge":
            if not (isinstance(key, str) and _KEY_RE.match(key)):
                raise ValueError("malformed proposal session key")
            out.update(action_id=aid, key=key, kind=pkind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["text"] = text(rec.get("text"), NUDGE_MAX)
            if rec.get("interrupt") is True:
                # #1960 Phase 2: HQ stopped the session's running turn first (with no text, it
                # only stopped it). A flag the card shows; never a value from a browser.
                out["interrupt"] = True
        elif pkind == "message":
            # A message to send (#1833): the card's connector and recipient labels and the exact
            # text. What a tap sends is the sidecar's entry, bound to this card by `binding`.
            bind = rec.get("binding")
            if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
                raise ValueError("a message card needs its binding")
            out.update(action_id=aid, kind=pkind, binding=bind)
            out["connector"] = text(rec.get("connector"), TITLE_MAX)
            out["recipient"] = text(rec.get("recipient"), TITLE_MAX)
            out["recipient_kind"] = text(rec.get("recipient_kind"), TITLE_MAX)
            # A Matrix room's member count, looked up live at staging (#1833): who would read it.
            members = rec.get("recipient_members")
            if isinstance(members, int) and not isinstance(members, bool) and 0 <= members <= 10**6:
                out["recipient_members"] = members
            out["text"] = text(rec.get("text"), MESSAGE_MAX)
            if "sender" in rec:
                # An email (#1836): the From and To addresses, and the subject and body the text
                # splits into, exactly as the transport sends them.
                for k, cap in (("sender", 254), ("address", 254), ("subject", 100)):
                    out[k] = text(rec.get(k), cap)
                out["body"] = text(rec.get("body"), MESSAGE_MAX)
        elif pkind == "operation":
            # An operation HQ proposed (#1559): the card's server-authored wording. What a tap
            # runs is the sidecar's entry, bound to this card by `binding` (`ask_operations`).
            op, risk, bind = rec.get("op"), rec.get("risk"), rec.get("binding")
            if not (isinstance(op, str) and _OP_RE.match(op)):
                raise ValueError("malformed operation id")
            if risk not in OPERATION_RISKS:
                raise ValueError("unknown operation risk")
            if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
                raise ValueError("an operation card needs its binding")
            out.update(action_id=aid, kind=pkind, op=op, risk=risk, binding=bind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["label"] = text(rec.get("label"), TITLE_MAX)
            out["verb"] = text(rec.get("verb"), OP_VERB_MAX)
            out["text"] = text(rec.get("text"), OP_SUMMARY_MAX)
            needs = rec.get("needs") or []
            if not isinstance(needs, list) or not set(needs) <= OPERATION_INPUTS:
                raise ValueError("unknown operation input")
            out["needs"] = list(needs)
        else:
            # A session to start: the project's name, the agent's name and the brief, for the
            # card. What a tap launches is the sidecar's entry (``ask_proposals``), never this.
            out.update(action_id=aid, kind=pkind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["agent"] = text(rec.get("agent"), TITLE_MAX)
            out["text"] = text(rec.get("text"), BRIEF_MAX)
    elif kind == "status":
        out["turn_id"] = _turn_id(rec)
        status = rec.get("status")
        if status not in TURN_STATUSES:
            raise ValueError("unknown turn status")
        out["status"] = status
        if "memory_capture" in rec:
            intent = rec["memory_capture"]
            if (
                status != "done"
                or not isinstance(intent, dict)
                or set(intent) != {"generation", "revision", "digest"}
                or any(
                    type(intent[k]) is not int or not 0 <= intent[k] < 10**12
                    for k in ("generation", "revision")
                )
                or not isinstance(intent["digest"], str)
                or not _DIGEST_RE.fullmatch(intent["digest"])
            ):
                raise ValueError("malformed memory capture intent")
            out["memory_capture"] = dict(intent)
        if rec.get("detail"):
            out["detail"] = text(rec.get("detail"), DETAIL_MAX)
        code = rec.get("code")
        if code is not None:
            if not (isinstance(code, str) and _ERROR_CODE_RE.match(code)):
                raise ValueError("malformed status code")
            out["code"] = code
        if "ctx" in rec:
            out["ctx"] = _admit_ctx(rec["ctx"])
        if "tainted" in rec:
            # The turn read agent- or outside-authored text (#2390): a boolean on a turn that ran.
            if status not in ("done", "failed", "stopped") or not isinstance(rec["tainted"], bool):
                raise ValueError("malformed tainted")
            out["tainted"] = rec["tainted"]
        # Step-list states this turn caused ITSELF (#2251: its own withdrawal or skip), taken up
        # by it: no continuation asks the Commander about what HQ just did and said.
        seen = rec.get("steps_seen")
        if seen is not None:
            if (
                not isinstance(seen, list)
                or len(seen) > STEPS_SEEN_MAX
                or not all(isinstance(k, str) and STEPS_KEY_RE.match(k) for k in seen)
            ):
                raise ValueError("malformed steps_seen")
            if seen:
                out["steps_seen"] = list(seen)
        cards = rec.get("cards_seen")
        if cards is not None:
            if (
                status != "done"
                or not isinstance(cards, dict)
                or len(cards) > CARDS_SEEN_MAX
                or not all(
                    isinstance(k, str)
                    and _ACTION_ID_RE.match(k)
                    and isinstance(v, str)
                    and _CARD_STATE_RE.match(v)
                    for k, v in cards.items()
                )
            ):
                raise ValueError("malformed cards_seen")
            if cards:
                out["cards_seen"] = dict(cards)
    elif kind == "summary":
        # A background summary of the exchanges up to and including turn `upto` (#2137 P2): HQ's
        # own words about the conversation, through the store boundary like any text, bound to
        # the exact prefix it summarised (`n` records, `digest` over them; `append_summary`).
        out["upto"] = _turn_id({"turn_id": rec.get("upto")})
        out["n"] = _bounded_int(rec.get("n"), "summary prefix", low=1)
        digest = rec.get("digest")
        if not (isinstance(digest, str) and _DIGEST_RE.match(digest)):
            raise ValueError("malformed summary digest")
        out["digest"] = digest
        out["text"] = text(rec.get("text"), SUMMARY_TEXT_MAX)
    elif kind == "assistant":
        out["turn_id"] = _turn_id(rec)
        out["text"] = text(rec.get("text"), TEXT_MAX)
        matches: list[dict] = []
        for m in (rec.get("matches") or [])[:MATCHES_MAX]:
            if not (isinstance(m, dict) and isinstance(m.get("id"), str)):
                raise ValueError("malformed match")
            if not _KEY_RE.match(m["id"]):
                raise ValueError("malformed match id")
            matches.append(
                {
                    "id": m["id"],
                    "title": text(m.get("title"), TITLE_MAX),
                    "why": text(m.get("why"), WHY_MAX),
                    # A session HQ only looked up (not one the Commander asked to find): kept
                    # for the next turn and for titles, never shown as a row (`_answer_matches`).
                    **({"hidden": True} if m.get("hidden") is True else {}),
                }
            )
        out["matches"] = matches
        mission_matches: list[dict] = []
        for m in (rec.get("mission_matches") or [])[:MATCHES_MAX]:
            mid = m.get("id") if isinstance(m, dict) else None
            if not (isinstance(mid, str) and missions.MISSION_ID_RE.match(mid)):
                raise ValueError("malformed mission match")
            state = m.get("state")
            mission_matches.append(
                {
                    "id": mid,
                    "title": text(m.get("title"), TITLE_MAX),
                    "state": state if state in missions.STATES else "",
                    "why": text(m.get("why"), WHY_MAX),
                }
            )
        out["mission_matches"] = mission_matches
        if rec.get("quiet") is True:
            # A waited call question HQ chose not to answer (#2354): the call says nothing.
            out["quiet"] = True
    elif kind == "update":
        # A followed session changed (#1443 Phase 4): an AUTHORED line, never model text.
        key, ukind = rec.get("key"), rec.get("kind")
        if not (isinstance(key, str) and _KEY_RE.match(key)):
            raise ValueError("malformed update session key")
        if ukind not in UPDATE_KINDS:
            raise ValueError("unknown update kind")
        eid = rec.get("event_id")
        if not (isinstance(eid, str) and _ACTION_ID_RE.match(eid)):
            raise ValueError("an update needs its event id")
        out.update(key=key, kind=ukind, event_id=eid)
        out["title"] = text(rec.get("title"), TITLE_MAX)
    elif kind == "tool":
        out["turn_id"] = _turn_id(rec)
        cid = rec.get("call_id")
        name = rec.get("name")
        if not (isinstance(cid, str) and _CALL_ID_RE.match(cid)):
            raise ValueError("malformed call id")
        if not (isinstance(name, str) and _TOOL_NAME_RE.match(name)):
            raise ValueError("malformed tool name")
        out["call_id"], out["name"] = cid, name
        args: dict = {}
        raw_args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
        for k, v in list(raw_args.items())[:ARGS_MAX_ITEMS]:
            if not (isinstance(k, str) and _TOOL_NAME_RE.match(k)):
                raise ValueError("malformed tool argument name")
            # Strings only, and every one through the guard: a number or a bool is model output
            # too (a numeric secret echoed as an argument), and no read tool takes one.
            if isinstance(v, str):
                args[k] = text(v, ARG_MAX)
        out["args"] = args
        meta_in = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        meta: dict = {}
        for k in ("count", "length", "rounds"):
            v = meta_in.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                meta[k] = v
        # #2076: the step's ordinal in its turn and how long it took, in milliseconds. Numbers.
        for k in ("n", "ms"):
            v = meta_in.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= STEP_NUMBER_MAX:
                meta[k] = v
        # …whether it had, or may have had, an effect (always stored, whatever its position),
        # and what became of a send's message: "nothing was sent" is read from these records.
        if meta_in.get("effect") is True:
            meta["effect"] = True
        # #2373: which path answered `find_sessions` — the index alone, the index's candidates
        # reranked by a model, or the model path (no index). Closed words only.
        if meta_in.get("source") in FIND_SOURCES:
            meta["source"] = meta_in["source"]
        sent = meta_in.get("sent")
        if sent is not None:
            if sent not in SENT_OUTCOMES:
                raise ValueError("unknown send outcome")
            meta["sent"] = sent
        if meta_in.get("keys"):
            meta["keys"] = _key_list(meta_in.get("keys"))
        if meta_in.get("missions"):
            meta["missions"] = _key_list(meta_in.get("missions"), missions_=True)
        err = meta_in.get("error")
        if err is not None:
            if not (isinstance(err, str) and _ERROR_CODE_RE.match(err)):
                raise ValueError("malformed tool error code")
            meta["error"] = err
        out["meta"] = meta
    elif kind == "screen":
        # What a turn asked the Commander's screen to show (#2102): a directive's closed fields
        # (an operation, a target by type and id, a section name) and nothing else. No free text.
        out["turn_id"] = _turn_id(rec)
        out.update(ask_screen.admit(rec))
    elif kind == "interjection":
        # The Commander's words heard on a call while HQ worked, carried into that turn (#2113 P3):
        # their own words, through the same boundary as a question's text.
        out["turn_id"] = _turn_id(rec)
        out["text"] = text(rec.get("text"), TEXT_MAX)
    elif kind in ("heard", "heard_link", "said", "counts"):
        out.update(_admit_call_record(kind, rec, text))
    elif kind == "steps_more":
        # The steps a turn ran past its stored ones (#2076): numbers and tool names only.
        out["turn_id"] = _turn_id(rec)
        for k in ("count", "ms"):
            v = rec.get(k)
            if not (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= STEP_NUMBER_MAX):
                raise ValueError("malformed step summary")
            out[k] = v
        for k in ("tools", "refused"):
            counts = rec.get(k) or {}
            if not isinstance(counts, dict) or len(counts) > STEPS_MORE_NAMES_MAX:
                raise ValueError("malformed step summary")
            admitted_counts: dict = {}
            for name, v in counts.items():
                if not (isinstance(name, str) and _TOOL_NAME_RE.match(name)):
                    raise ValueError("malformed tool name")
                if not (
                    isinstance(v, int) and not isinstance(v, bool) and 0 < v <= STEP_NUMBER_MAX
                ):
                    raise ValueError("malformed step summary")
                admitted_counts[name] = v
            out[k] = admitted_counts
    return out


def _admit_ctx(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"tokens", "estimated", "window"}:
        raise ValueError("malformed context size")
    tokens, window = raw.get("tokens"), raw.get("window")
    if not (
        isinstance(tokens, int) and not isinstance(tokens, bool) and 0 <= tokens <= CTX_NUMBER_MAX
    ):
        raise ValueError("malformed context size")
    out: dict = {"tokens": tokens, "estimated": raw.get("estimated") is True}
    if window is not None:
        if not (
            isinstance(window, int)
            and not isinstance(window, bool)
            and 0 < window <= CTX_NUMBER_MAX
        ):
            raise ValueError("malformed context window")
        out["window"] = window
    return out


def records_digest(records: list[dict]) -> str:
    """What binds a summary to the exact records it summarised (#2137 P2): a digest over their
    canonical JSON, the same however the file is read."""
    import hashlib

    blob = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def append_summary(cid: str, rec: dict) -> dict | None:
    """Publish a summary record ONLY if the conversation still begins with the exact records it
    summarised (`rec["n"]` of them, `rec["digest"]` over them), it holds fewer than
    `SUMMARIES_MAX` summaries, and none of them covers as much already. Checked and written under
    the file's own lock, in one hold: records appended meanwhile stay after it. Returns the record
    as written, or None when the prefix changed (deleted, swept, rewritten) or a summary already
    covers it."""
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = _admit({**rec, "type": "summary"}, secrets)
        try:
            fd = os.open(_path(cid), os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            chunks = []
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > READ_MAX_BYTES:
                    return None
            records = _parse(b"".join(chunks))
            n = admitted["n"]
            if len(records) < n or records_digest(records[:n]) != admitted["digest"]:
                return None
            held = [r for r in records if r.get("type") == "summary"]
            if len(held) >= SUMMARIES_MAX or any(r.get("n", 0) >= n for r in held):
                return None
            _write_all(fd, _records_bytes([admitted]))
        finally:
            os.close(fd)
    return admitted


def _bounded_int(value: object, what: str, *, low: int = 0) -> int:
    if not (isinstance(value, int) and not isinstance(value, bool) and low <= value):
        raise ValueError(f"malformed {what}")
    if value > HEARD_NUMBER_MAX:
        raise ValueError(f"malformed {what}")
    return value


def _admit_call_record(kind: str, rec: dict, text: Callable[[object, int], str]) -> dict:
    """A call's own records (#2137), field by field. Free text is the Commander's kept words (a
    `heard` routed anywhere but `dropped`) or HQ's own spoken line (`said`), both through the store
    boundary (`guard_text`: redacted and checked WHOLE, then capped). A dropped `heard` refuses any
    text: the words of an utterance the call did not take are never stored."""
    out: dict = {}
    if kind in ("heard", "heard_link"):
        hid = rec.get("heard_id")
        if not (isinstance(hid, str) and _HEARD_ID_RE.match(hid)):
            raise ValueError("malformed heard id")
        out["heard_id"] = hid
    if kind == "heard":
        route = rec.get("route")
        if route not in ("received", "dropped"):
            raise ValueError("a heard record is received or dropped")
        out["route"] = route
        turn = rec.get("turn")
        if turn is not None:
            out["turn"] = _bounded_int(turn, "turn number")
        out["chars"] = _bounded_int(rec.get("chars"), "length")
        lang = rec.get("lang")
        if lang is not None:
            if not (isinstance(lang, str) and _LANG_RE.match(lang)):
                raise ValueError("malformed language")
            out["lang"] = lang
        if rec.get("late") is True:
            out["late"] = True
        if route == "dropped":
            why = rec.get("why")
            if why not in HEARD_DROPS:
                raise ValueError("unknown drop reason")
            if "text" in rec:
                raise ValueError("a dropped utterance keeps no words")
            out["why"] = why
        else:
            out["text"] = text(rec.get("text"), TEXT_MAX)
    elif kind == "heard_link":
        route = rec.get("route")
        if route not in HEARD_ROUTES - {"dropped"}:
            raise ValueError("unknown heard route")
        out["route"] = route
        tid = rec.get("turn_id")
        if tid is not None:
            out["turn_id"] = _turn_id(rec)
        _admit_card_ref(rec, out)
    elif kind == "said":
        skind = rec.get("kind")
        if skind not in SAID_KINDS:
            raise ValueError("unknown said kind")
        out["kind"] = skind
        out["text"] = text(rec.get("text"), TEXT_MAX)
        if rec.get("turn_id") is not None:
            out["turn_id"] = _turn_id(rec)
        _admit_card_ref(rec, out)
    else:  # counts
        for k in ("heard_dropped", "heard_said"):
            out[k] = _bounded_int(rec.get(k, 0), "count")
    return out


def _admit_card_ref(rec: dict, out: dict) -> None:
    """The card a call's decision or outcome was about (#2372): a card id, nothing else. It lets
    HQ's history correlate the Commander's spoken decision with that card's outcomes."""
    card = rec.get("card")
    if card is None:
        return
    if not (isinstance(card, str) and _ACTION_ID_RE.match(card)):
        raise ValueError("malformed card id")
    out["card"] = card


def _records_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode(
        "utf-8", "replace"
    )


def _write_all(fd: int, data: bytes) -> None:
    size = os.fstat(fd).st_size
    # A torn tail (a crash mid-record) is terminated first, so the next record is not glued onto
    # it and lost with it; the torn line stays behind as one the reader skips.
    if size and os.pread(fd, 1, size - 1) != b"\n":
        data = b"\n" + data
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if not n:
            raise OSError("conversation append made no progress")
        view = view[n:]
    os.fsync(fd)


def create(cid: str | None = None) -> str:
    """A new, empty conversation (just its header). Returns its id."""
    _ensure_root()
    cid = cid or new_cid()
    path = _path(cid)
    header = _admit({"type": "conversation", "id": cid}, [])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, _records_bytes([header]))
    finally:
        os.close(fd)
    _sync_dir(root())
    return cid


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(cid: str, *records: dict) -> list[dict]:
    """Append records through the store boundary, durably and under the file's own lock. Returns
    the records exactly as written. Raises ConversationNotFound, StoreRefused or ValueError —
    in every case before a byte is written."""
    if not records:
        return []
    # Snapshot, admission and write under ONE hold of the registration fence: a secret registered
    # concurrently is known either before the snapshot (redacted here) or only after the write.
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = [_admit(r, secrets) for r in records]
        try:
            fd = os.open(_path(cid), os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ConversationNotFound(cid) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _write_all(fd, _records_bytes(admitted))
        finally:
            os.close(fd)
    return admitted


def append_if(cid: str, decide: Callable[[list[dict]], Sequence[dict]]) -> list[dict]:
    """Decide and append in ONE step, under the file's own lock (#2103): ``decide`` is given the
    conversation's records as they are with the lock held and returns the records to append
    (none: nothing is written), or raises to refuse. Nothing can be appended between its look
    and the write, so a check ("is there room in the queue?", "is this id unused?") and the
    record that makes it true are one step, also while a running turn holds the cross-process
    TURN lock, which this does not take. Returns the records as written. Blocking."""
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        try:
            fd = os.open(_path(cid), os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ConversationNotFound(cid) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size > READ_MAX_BYTES:
                raise StoreRefused("this conversation is too large to read — start a new one")
            records = list(decide(_parse(os.pread(fd, READ_MAX_BYTES, 0))))
            if not records:
                return []
            admitted = [_admit(r, secrets) for r in records]
            _write_all(fd, _records_bytes(admitted))
        finally:
            os.close(fd)
    return admitted


def read(cid: str) -> list[dict]:
    """Every readable record, in order. A torn or malformed line is skipped, never fatal."""
    try:
        fd = os.open(_path(cid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConversationNotFound(cid) from None
    try:
        if os.fstat(fd).st_size > READ_MAX_BYTES:
            # Unreachable by construction (MAX_TURNS of worst-case turns fit); if it ever happens
            # the conversation says so rather than silently losing its newest turns.
            raise StoreRefused("this conversation is too large to read — start a new one")
        data = os.read(fd, READ_MAX_BYTES)
    finally:
        os.close(fd)
    return _parse(data)


def _parse(data: bytes) -> list[dict]:
    out: list[dict] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") in RECORD_TYPES:
            out.append(rec)
    return out


def exists(cid: str) -> bool:
    return valid_cid(cid) and _path(cid).is_file()


# ---- the turn lock -----------------------------------------------------------------------------


def try_turn_lock(cid: str) -> int | None:
    """The conversation's turn lock, non-blocking: an fd to hold (and close to release), or None
    when another process — or another turn in this one — already holds it."""
    _ensure_root()
    fd = os.open(_lock_path(cid), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def turn_lock(cid: str) -> Iterator[bool]:
    """``try_turn_lock`` as a context: yields whether it was acquired."""
    fd = try_turn_lock(cid)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _open_lock(path: Path) -> int:
    _ensure_root()
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)


@contextlib.contextmanager
def proposals_lock(cid: str) -> Iterator[None]:
    """The proposal sidecar's lock, BLOCKING and short: held across one read-modify-write of
    ``proposals/<cid>.json``, and by retention across its last look and the delete. Lock order:
    the turn lock, then the decide lock, then this one."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_proposals_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def try_decide_lock(cid: str) -> int | None:
    """Held for as long as a launch from this conversation runs (``ask_proposals``), non-blocking:
    an fd to hold and close, or None while another launch — in this process or another — holds
    it. A ``deciding`` proposal whose lock is free is one nobody is still deciding."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_decide_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def try_steps_lock(cid: str) -> int | None:
    """The step-list fence (#2251), non-blocking: an fd to hold and close, or None while another
    holder — in this process or another — has it. Held across a whole step-list transition: the
    fence check, the staging and the membership of a card; a revision's plan, its withdrawals and
    its write; End steps. Lock order: the turn lock, this one, the decide lock, the proposals
    lock — so a withdrawal through the decide route inside it never inverts the order."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_steps_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---- the conversation list ---------------------------------------------------------------------


def _head(path: Path) -> list[dict]:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = os.read(fd, _HEAD_BYTES)
    finally:
        os.close(fd)
    # The last line of a partial head may be cut; `_parse` skips it.
    return _parse(data)


def _conversation_files() -> list[tuple[str, Path, float]]:
    """``(cid, path, mtime)`` for every conversation, newest-changed first."""
    base = root()
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    out: list[tuple[str, Path, float]] = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        cid = name[: -len(".jsonl")]
        if not valid_cid(cid):
            continue
        path = base / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append((cid, path, st.st_mtime))
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def summary(cid: str, path: Path, mtime: float) -> dict:
    """What the switcher lists: the id, a title (the first question, already through the store
    boundary when it was written) and when it was created and last changed."""
    created = mtime
    title = ""
    try:
        head = _head(path)
    except OSError:
        head = []
    for rec in head:
        if rec.get("type") == "conversation" and isinstance(rec.get("ts"), int | float):
            created = float(rec["ts"])
        if rec.get("type") == "user" and isinstance(rec.get("text"), str):
            title = " ".join(rec["text"].split())[:LIST_TITLE_MAX]
            break
    return {"id": cid, "title": title, "created_at": created, "updated_at": mtime}


def list_conversations() -> list[dict]:
    return [summary(cid, path, mtime) for cid, path, mtime in _conversation_files()]


# ---- the session-proposal sidecar (#1443 Phase 3a) ---------------------------------------------

#: A session proposal's life. ``pending`` waits for a tap; ``deciding`` is a launch in flight; every
#: other state is settled and never left again — an uncertain launch is never retried.
SESSION_PROPOSAL_STATES = frozenset(
    {
        "pending",
        "deciding",
        "started",
        "unconfirmed",
        "failed",
        "stale",
        "dismissed",
        "interrupted",
    }
)
#: Proposals one conversation may hold. A turn has no cap of its own (#2076): at this limit
#: `ask_orchestrator` refuses a propose tool before it writes anything, and the operator starts
#: a new conversation for more.
MAX_SESSION_PROPOSALS = MAX_TURNS * 3
NOTICES = frozenset({"", "default_changed", "busy", "session_limit"})
_ENGINE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
#: A session proposal started FROM a specialist (#1910): its id (`specialists_store.ID_RE`) and the
#: version the card showed (`specialist_launch.VERSION_MAX`).
_SPECIALIST_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")
SPECIALIST_VERSION_MAX = 999_999
PATH_MAX = 4_096
#: The reason a pending brief becomes ``stale`` when a secret it contains is registered later.
BRIEF_NOW_SECRET = "the brief now contains a secret, so it will not be sent — ask again"  # noqa: S105 — a message, not a credential


class SidecarUnreadable(Exception):
    """The proposal sidecar exists and cannot be read: nothing in it can be decided."""


class Withheld(Exception):
    """A new proposal whose text the boundary would change: it is refused, never stored altered —
    a redacted brief would be a different launch from the one shown."""


def ensure_proposals_dir() -> Path:
    _ensure_root()
    d = root() / "proposals"
    _ensure_dir(d)
    return d


def has_proposals(cid: str) -> bool:
    """Whether the conversation has a card sidecar at all (#2050): one without has no card state
    to read. A path that cannot be checked counts as present, so its read says what it is."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        os.lstat(_proposals_path(cid))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def read_proposals(cid: str) -> list[dict]:
    """The sidecar's entries, in order; ``[]`` when there is none. SidecarUnreadable otherwise —
    never a partial list, so nothing is decided on a guess."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        fd = os.open(_proposals_path(cid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SidecarUnreadable(type(e).__name__) from None
    try:
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
            if len(raw) > READ_MAX_BYTES:
                raise SidecarUnreadable("too large")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError:
        raise SidecarUnreadable("not JSON") from None
    rows = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SidecarUnreadable("malformed")
    return rows


def _admit_entry(e: dict, secrets: list[str], *, fresh: bool) -> dict:
    """One sidecar entry as it may be written; ValueError for a shape no caller should produce.

    Any kind may carry ``replaced_by`` (#2120): the id of the card HQ staged to replace it. It is
    kept only on a ``dismissed`` entry (a replacement dismisses the card it replaces, and nothing
    else does). The replacement carries ``replaces``, the card it replaced. Both are absent
    otherwise, so an older sidecar reads byte for byte the same."""
    out = _admit_kind(e, secrets, fresh=fresh)
    for key in ("replaced_by", "replaces"):
        other = e.get(key)
        if other is None:
            continue
        if not (isinstance(other, str) and _ACTION_ID_RE.match(other)) or other == out["id"]:
            raise ValueError(f"malformed {key}")
        if key == "replaced_by" and out.get("state") != "dismissed":
            raise ValueError("only a dismissed card was replaced")
        out[key] = other
    return out


def _admit_kind(e: dict, secrets: list[str], *, fresh: bool) -> dict:
    pid, tid = e.get("id"), e.get("turn_id")
    if not (isinstance(pid, str) and _ACTION_ID_RE.match(pid)):
        raise ValueError("malformed proposal id")
    if not valid_turn_id(tid):
        raise ValueError("malformed turn id")
    if e.get("kind") == "operation":
        return _admit_operation(e, pid, tid, secrets, fresh=fresh)
    if e.get("kind") == "message":
        return _admit_message(e, pid, tid, secrets)
    if e.get("kind") != "session":
        raise ValueError("unknown proposal kind")
    state = e.get("state")
    if state not in SESSION_PROPOSAL_STATES:
        raise ValueError("unknown proposal state")
    engine, project_id, cwd = e.get("engine"), e.get("project_id"), e.get("cwd")
    if not (isinstance(engine, str) and _ENGINE_RE.match(engine)):
        raise ValueError("malformed engine id")
    if not (isinstance(project_id, str) and _PROJECT_ID_RE.match(project_id)):
        raise ValueError("malformed project id")
    # The project's own folder, from the project store — a path, not model text.
    if not (
        isinstance(cwd, str)
        and cwd.startswith("/")
        and len(cwd) <= PATH_MAX
        and not any(c in cwd for c in "\x00\n\r")
    ):
        raise ValueError("malformed project folder")
    # Where the folder RESOLVED when the card was shown: a start compares against it, so a
    # symlink repointed before the tap is refused rather than taken as the new baseline.
    real = e.get("real")
    if not (
        isinstance(real, str)
        and real.startswith("/")
        and len(real) <= PATH_MAX
        and not any(c in real for c in "\x00\n\r")
    ):
        raise ValueError("malformed resolved folder")
    for k in ("bypass_default",):
        if not isinstance(e.get(k), bool):
            raise ValueError(f"{k} must be a boolean")
    bypass = e.get("bypass")
    if bypass is not None and not isinstance(bypass, bool):
        raise ValueError("bypass must be a boolean")
    skey = e.get("session_key") or ""
    if skey and not (isinstance(skey, str) and _KEY_RE.match(skey)):
        raise ValueError("malformed session key")
    notice = e.get("notice") or ""
    if notice not in NOTICES:
        raise ValueError("unknown notice")
    out = {
        "id": pid,
        "turn_id": tid,
        "kind": "session",
        "state": state,
        "engine": engine,
        "project_id": project_id,
        "cwd": cwd,
        "real": real,
        "bypass_default": e["bypass_default"],
        "bypass": bypass,
        "session_key": skey,
        "notice": notice,
        "project": guard_text(e.get("project"), TITLE_MAX, secrets=secrets),
        "agent": guard_text(e.get("agent"), TITLE_MAX, secrets=secrets),
        "reason": guard_text(e.get("reason") or "", DETAIL_MAX, secrets=secrets),
    }
    # Optional (#1910): absent on every entry before it, and then not written at all, so a sidecar
    # without one is byte-for-byte what an older build reads.
    spec = e.get("specialist")
    if spec is not None:
        sid, n = (spec.get("id"), spec.get("version")) if isinstance(spec, dict) else (None, None)
        if not (
            isinstance(spec, dict)
            and set(spec) == {"id", "version"}
            and isinstance(sid, str)
            and _SPECIALIST_ID_RE.match(sid)
            and isinstance(n, int)
            and not isinstance(n, bool)
            and 1 <= n <= SPECIALIST_VERSION_MAX
        ):
            raise ValueError("malformed specialist")
        out["specialist"] = {"id": sid, "version": n}
    # Optional (#2014): the kind of session the card starts, pinned when it was shown. Only an API
    # entry carries it ("api"); absent is a terminal entry, so an older sidecar reads unchanged.
    runtime = e.get("runtime")
    if runtime is not None:
        if runtime != "api":
            raise ValueError("unknown session runtime")
        out["runtime"] = "api"
    for k in ("ts", "decided_at", "settled_at"):
        v = e.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    _admit_by_hq(e, out)
    brief = e.get("brief")
    if not isinstance(brief, str) or not brief:
        raise ValueError("a session proposal needs its brief")
    guarded = guard_text(brief, BRIEF_MAX, secrets=secrets)
    if guarded != brief:
        if fresh:
            raise Withheld("the brief contains a secret or looks like a credential")
        # An entry written earlier whose brief NOW matches a secret registered since: kept only
        # in its scrubbed form, and a pending one can no longer be started — what would launch is
        # no longer what was shown.
        if out["state"] == "pending":
            out["state"], out["reason"] = "stale", BRIEF_NOW_SECRET
    out["brief"] = guarded
    return out


def _admit_values(raw: object, secrets: list[str], *, fresh: bool, what: str) -> dict:
    """An operation's typed fields or facts: short keys; str (guarded), int, bool or None values.
    A NEW entry whose text the boundary would change is refused (Withheld), never stored altered."""
    if not isinstance(raw, dict) or len(raw) > OP_FIELDS_MAX:
        raise ValueError(f"malformed operation {what}")
    out: dict = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and _OP_FIELD_RE.match(k)):
            raise ValueError(f"malformed operation {what} name")
        if isinstance(v, str):
            guarded = guard_text(v, OP_SUMMARY_MAX, secrets=secrets)
            if guarded != v and fresh:
                raise Withheld(f"the operation's {what} contain a secret or look like a credential")
            out[k] = guarded
        elif v is None or isinstance(v, bool) or (isinstance(v, int) and abs(v) < 10**12):
            out[k] = v
        else:
            raise ValueError(f"malformed operation {what} value")
    return out


def _admit_by_hq(e: dict, out: dict) -> None:
    """HQ ran it on the Commander's Authority setting (#2390; a session start or a connector
    message too, #2391): who decided, and the authority revision the act was admitted under.
    Absent on a card the Commander decided; anything but these exact shapes is dropped (a card
    returned to pending writes them as None)."""
    if e.get("decided_by") == "hq":
        out["decided_by"] = "hq"
        rev = e.get("authority_rev")
        if type(rev) is int and 0 <= rev < 10**12:
            out["authority_rev"] = rev


def _admit_message(e: dict, pid: str, tid: str, secrets: list[str]) -> dict:
    """One message entry (#1833). The text is guarded like every stored text: a secret registered
    after staging is redacted HERE, which changes the text, which breaks the card's binding, so
    the tap finds it `stale` and sends nothing. A stored text is never a secret, and a tap never
    sends anything but the stored text."""
    state, iid, gen, bind = (
        e.get("state"),
        e.get("instance_id"),
        e.get("generation"),
        e.get("binding"),
    )
    if state not in MESSAGE_STATES:
        raise ValueError("unknown message state")
    if not (isinstance(iid, str) and _CONNECTOR_ID_RE.match(iid)):
        raise ValueError("malformed connector id")
    if not (type(gen) is int and gen > 0):
        raise ValueError("malformed connector generation")
    if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
        raise ValueError("a message needs its binding")
    rec = e.get("recipient")
    from .connectors import store as connector_store

    email = "sender" in e
    if not (
        isinstance(rec, dict)
        and set(rec) == {"id", "label", "kind"}
        and isinstance(rec["id"], str)
        and (
            connector_store.valid_address(rec["id"]) and rec["kind"] == "email"
            if email
            else _RECIPIENT_ID_RE.match(rec["id"]) and rec["kind"] in _RECIPIENT_KINDS
        )
    ):
        raise ValueError("malformed recipient")
    # An email entry (#1836) also carries the From address it was proposed with.
    if email and not connector_store.valid_address(e["sender"]):
        raise ValueError("malformed sender")
    body = e.get("text")
    if not isinstance(body, str) or not body or len(body) > MESSAGE_MAX:
        raise ValueError("malformed message text")
    out = {
        "id": pid,
        "turn_id": tid,
        "kind": "message",
        "state": state,
        "instance_id": iid,
        "generation": gen,
        "binding": bind,
        "connector": guard_text(e.get("connector") or "", TITLE_MAX, secrets=secrets),
        "recipient": {
            "id": rec["id"],
            "label": guard_text(rec.get("label") or "", TITLE_MAX, secrets=secrets),
            "kind": rec["kind"],
        },
        "text": guard_text(body, MESSAGE_MAX, secrets=secrets),
        **({"sender": e["sender"]} if email else {}),
        # Short codes and the provider's id, from this server's own transport; never free text.
        "code": e["code"][:40] if isinstance(e.get("code"), str) else "",
        "message_id": e["message_id"][:255] if isinstance(e.get("message_id"), str) else "",
        "reason": guard_text(e.get("reason") or "", DETAIL_MAX, secrets=secrets),
    }
    for k in ("ts", "decided_at", "settled_at"):
        v = e.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    _admit_by_hq(e, out)
    return out


#: An `assemble` card's display rows (#2130): at most one per step a plan can hold —
#: `mission_plan_document.MAX_OBJECTIVES` × `playbooks.schema.MAX_STEPS` (pinned by a test).
TEAM_ROWS_MAX = 600
TEAM_ROW_STATES = frozenset({"assembled", "kept", "unassigned"})
_TEAM_STEP_RE = re.compile(r"\Ao[1-9][0-9]?\.[a-z][a-z0-9_-]{0,31}\Z")  # mission_team.STEP_ID_RE
_TEAM_SPEC_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")  # specialists_store.ID_RE
_TEAM_ENGINE_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,63}\Z")
_TEAM_MODEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,95}\Z")  # manifest._MODEL_ID_RE
_TEAM_REASON_MAX = 200  # mission_team.REASON_MAX


def _admit_display(raw: object, secrets: list[str], *, fresh: bool) -> dict | None:
    """An operation's display-only payload (#2130): `{revision, team: [row…]}` — the rows of the
    team an `assemble` card was STAGED with, in plan order. Its own validator, so the scalar
    facts stay scalar. Never executable: the tap builds its request from fields and facts only.

    EVERY string in it goes through the store boundary after its own shape check (Hermes on
    #2226: an identifier grammar does not exclude a registered secret's value). The identifiers
    are checked against the REGISTERED secrets (`template_secrets.redact_text`, the boundary's
    first half): they are server-validated against strict grammars, and the boundary's
    credential-SHAPE heuristic, meant for free text, flags real ids (`claude-opus-4-5-20251101`).
    An unassigned step's `reason`, free text, gets the whole boundary (`guard_text`). A NEW card
    whose display the boundary would change is refused (`Withheld`), never stored altered. A
    STORED one (a secret registered after staging) is WITHHELD whole: None, so the card shows its
    sentence and no rows — never the value, and never an identifier rewritten into another — and,
    its team hidden, offers no Confirm (`ask_operations.team_hidden`). That happens at the
    conversation's next sidecar write AND on every read before it: the view re-admits the stored
    display through this function (`ask_operations.shown_display`, Hermes on #2226)."""
    if not isinstance(raw, dict) or set(raw) != {"revision", "team"}:
        raise ValueError("malformed operation display")
    rev, team = raw["revision"], raw["team"]
    if not (type(rev) is int and 0 < rev < 10**9):
        raise ValueError("malformed operation display revision")
    if not isinstance(team, list) or len(team) > TEAM_ROWS_MAX:
        raise ValueError("malformed operation display team")
    changed = False

    def flag(out: str, value: str) -> str:
        nonlocal changed
        if out != value:
            if fresh:
                raise Withheld("the team contains a secret or looks like a credential")
            changed = True
        return out

    def ident(value: str) -> str:
        try:
            return flag(template_secrets.redact_text(value, secrets), value)
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None

    def text(value: str, cap: int) -> str:
        return flag(guard_text(value, cap, secrets=secrets), value)

    rows: list[dict] = []
    for r in team:
        if not isinstance(r, dict) or r.get("state") not in TEAM_ROW_STATES:
            raise ValueError("malformed team row")
        step = r.get("step")
        if not (isinstance(step, str) and _TEAM_STEP_RE.match(step)):
            raise ValueError("malformed team row step")
        if r["state"] == "unassigned":
            if set(r) != {"step", "state", "reason"} or not isinstance(r["reason"], str):
                raise ValueError("malformed unassigned team row")
            rows.append(
                {
                    "step": ident(step),
                    "state": "unassigned",
                    "reason": text(r["reason"], _TEAM_REASON_MAX),
                }
            )
            continue
        keys = {"step", "state", "specialist", "version", "engine", "model"}
        if (
            set(r) != keys
            or not (isinstance(r["specialist"], str) and _TEAM_SPEC_RE.match(r["specialist"]))
            or not (type(r["version"]) is int and 0 < r["version"] < 10**9)
            # Empty when the pick's specialist version is gone (an archived kept pick).
            or not (
                isinstance(r["engine"], str)
                and (r["engine"] == "" or _TEAM_ENGINE_RE.match(r["engine"]))
            )
            or not (isinstance(r["model"], str) and _TEAM_MODEL_RE.match(r["model"]))
        ):
            raise ValueError("malformed team row pick")
        rows.append(
            {
                "step": ident(step),
                "state": r["state"],
                "specialist": ident(r["specialist"]),
                "version": r["version"],
                "engine": ident(r["engine"]) if r["engine"] else "",
                "model": ident(r["model"]),
            }
        )
    return None if changed else {"revision": rev, "team": rows}


def _admit_operation(e: dict, pid: str, tid: str, secrets: list[str], *, fresh: bool) -> dict:
    """One HQ operation entry (#1559). Method, path and body are never stored: the catalog rebuilds
    them at the tap, so this shape cannot name a route."""
    state, op, target, bind = e.get("state"), e.get("op"), e.get("target"), e.get("binding")
    if state not in OPERATION_STATES:
        raise ValueError("unknown operation state")
    if not (isinstance(op, str) and _OP_RE.match(op)):
        raise ValueError("malformed operation id")
    if not (
        isinstance(target, str)
        and (
            target == ""
            or _KEY_RE.match(target)
            or _PROJECT_ID_RE.match(target)
            or (
                op in {"memory.edit", "memory.delete"}
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", target)
            )
        )
    ):
        raise ValueError("malformed operation target")
    if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
        raise ValueError("an operation needs its binding")
    out = {
        "id": pid,
        "turn_id": tid,
        "kind": "operation",
        "state": state,
        "op": op,
        "target": target,
        "binding": bind,
        "fields": _admit_values(e.get("fields") or {}, secrets, fresh=fresh, what="fields"),
        "facts": _admit_values(e.get("facts") or {}, secrets, fresh=fresh, what="facts"),
        "title": guard_text(e.get("title") or "", TITLE_MAX, secrets=secrets),
        "outcome": guard_text(e.get("outcome") or "", OP_SUMMARY_MAX, secrets=secrets),
        "reason": guard_text(e.get("reason") or "", DETAIL_MAX, secrets=secrets),
    }
    if e.get("display") is not None:
        display = _admit_display(e["display"], secrets, fresh=fresh)
        if display is not None:
            out["display"] = display
    for k in ("ts", "decided_at", "settled_at"):
        v = e.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    _admit_by_hq(e, out)
    # What a move committed (#1786): the Library record it created and that record's file
    # revision — the only proof a resumed move accepts, never a name or matching bytes.
    committed = e.get("committed")
    if committed is not None:
        # rev 0: created, its upload not yet confirmed. digest: a KEYED digest of the bytes
        # moved (`template_secrets.keyed_digest`), never the bytes and never a bare hash.
        if not (
            isinstance(committed, dict)
            and set(committed) == {"id", "rev", "digest"}
            and isinstance(committed["id"], str)
            and re.fullmatch(r"[0-9a-f]{32}", committed["id"])
            and type(committed["rev"]) is int
            and 0 <= committed["rev"] <= 10**9
            and isinstance(committed["digest"], str)
            and _DIGEST_RE.match(committed["digest"])
        ):
            raise ValueError("malformed committed destination")
        out["committed"] = {k: committed[k] for k in ("id", "rev", "digest")}
    # The draft a mission card created (#1950): the id its route returned, for the card's link and
    # HQ's continuation. A mission id, never anything else.
    mid = e.get("mission_id")
    if mid is not None:
        if not (isinstance(mid, str) and missions.MISSION_ID_RE.match(mid)):
            raise ValueError("malformed mission id")
        out["mission_id"] = mid
    return out


def write_proposals(cid: str, rows: list[dict], *, fresh: str | None = None) -> list[dict]:
    """Replace the sidecar, atomically and durably, through the store boundary. The CALLER holds
    :func:`proposals_lock`. ``fresh`` names a NEW entry, which is refused (Withheld) rather than
    stored altered. Returns the entries as written."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    if len(rows) > MAX_SESSION_PROPOSALS:
        raise StoreRefused("this conversation holds as many proposals as it can")
    d = ensure_proposals_dir()
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = [_admit_entry(r, secrets, fresh=r.get("id") == fresh) for r in rows]
        data = json.dumps({"proposals": admitted}, ensure_ascii=False).encode("utf-8", "replace")
        tmp = d / f".{cid}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, _proposals_path(cid))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _sync_dir(d)
    return admitted


# ---- the step-list sidecar (#2251) ---------------------------------------------------------------
#
# HQ's ordered steps for a multi-part request: ``steps/<cid>.json``, written under the proposal
# sidecar's lock (`proposals_lock`), so a tap, a revision and End steps never interleave. It
# holds STRUCTURE only: titles, order, needs, which cards belong to which step, skipped and ended.
# Every step's state is derived from its cards' states in the proposal sidecar (`ask_steps`), so
# there is no second copy of an outcome to drift and no write that spans two files.

STEP_LISTS_MAX = 20
STEPS_PER_LIST_MAX = 12
STEP_CARDS_MAX = 24
STEP_TITLE_MAX = 120
STEP_DETAIL_MAX = 300
STEP_LIST_ID_RE = re.compile(r"^[0-9a-f]{16}$")
STEP_ID_RE = re.compile(r"^s[0-9a-f]{8}$")
#: A step's card kind (`ask_orchestrator._card_kind`): a tool name, `library:<group>`, `team`.
STEP_KIND_RE = re.compile(r"^[a-z_]{1,30}(:[a-z_]{1,20})?$")


def read_steps(cid: str) -> list[dict]:
    """The conversation's step lists, oldest first; ``[]`` when there is none. SidecarUnreadable
    otherwise — never a partial list."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        fd = os.open(_steps_path(cid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SidecarUnreadable(type(e).__name__) from None
    try:
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
            if len(raw) > READ_MAX_BYTES:
                raise SidecarUnreadable("too large")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError:
        raise SidecarUnreadable("not JSON") from None
    lists = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(lists, list) or not all(isinstance(x, dict) for x in lists):
        raise SidecarUnreadable("malformed")
    return lists


def _admit_step_list(raw: dict, secrets: list[str]) -> dict:
    """One step list as it may be written; ValueError for a shape no caller should produce."""
    lid = raw.get("id")
    if not (isinstance(lid, str) and STEP_LIST_ID_RE.match(lid)):
        raise ValueError("malformed step list id")
    rev = raw.get("revision")
    if not (isinstance(rev, int) and not isinstance(rev, bool) and 1 <= rev < 10**9):
        raise ValueError("malformed step list revision")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= STEPS_PER_LIST_MAX:
        raise ValueError("malformed steps")
    seen: list[str] = []
    cards_seen: set[str] = set()
    out_steps: list[dict] = []
    for st in steps:
        if not isinstance(st, dict):
            raise ValueError("malformed step")
        sid = st.get("sid")
        if not (isinstance(sid, str) and STEP_ID_RE.match(sid)) or sid in seen:
            raise ValueError("malformed step id")
        needs = st.get("needs") or []
        # A step needs only steps BEFORE it: no forward dependency, no cycle, by construction.
        if not isinstance(needs, list) or any(n not in seen for n in needs):
            raise ValueError("a step may only need steps before it")
        cards = st.get("cards") or []
        retired = st.get("retired") or []
        for group in (cards, retired):
            if not isinstance(group, list) or len(group) > STEP_CARDS_MAX:
                raise ValueError("malformed step cards")
            for c in group:
                if not (isinstance(c, str) and _ACTION_ID_RE.match(c)) or c in cards_seen:
                    raise ValueError("malformed step card")
                cards_seen.add(c)
        row = {
            "sid": sid,
            "title": guard_text(st.get("title"), STEP_TITLE_MAX, secrets=secrets),
            "detail": guard_text(st.get("detail") or "", STEP_DETAIL_MAX, secrets=secrets),
            "needs": list(dict.fromkeys(needs)),
            "cards": list(cards),
            "retired": list(retired),
            "skipped": st.get("skipped") is True,
        }
        kind = st.get("kind")
        if kind is not None:
            if not (isinstance(kind, str) and STEP_KIND_RE.match(kind)):
                raise ValueError("malformed step kind")
            row["kind"] = kind
        if not row["title"]:
            raise ValueError("a step needs a title")
        for k in ("skipped_at",):
            v = st.get(k)
            if isinstance(v, int | float) and not isinstance(v, bool):
                row[k] = float(v)
        seen.append(sid)
        out_steps.append(row)
    out: dict = {
        "id": lid,
        "revision": rev,
        "title": guard_text(raw.get("title"), STEP_TITLE_MAX, secrets=secrets),
        "ended": raw.get("ended") is True,
        "steps": out_steps,
    }
    for k in ("drawn_at_turn", "created_turn"):
        v = raw.get(k)
        if not valid_turn_id(v):
            raise ValueError(f"malformed {k}")
        out[k] = v
    for k in ("ts", "ended_at"):
        v = raw.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    return out


def write_steps(cid: str, lists: list[dict]) -> list[dict]:
    """Replace the step-list sidecar, atomically and durably, through the store boundary. The
    CALLER holds :func:`proposals_lock`. Returns the lists as written."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    if len(lists) > STEP_LISTS_MAX:
        raise StoreRefused("this conversation holds as many step lists as it can")
    _ensure_root()
    d = root() / "steps"
    _ensure_dir(d)
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = [_admit_step_list(x, secrets) for x in lists]
        data = json.dumps({"lists": admitted}, ensure_ascii=False).encode("utf-8", "replace")
        tmp = d / f".{cid}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, _steps_path(cid))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _sync_dir(d)
    return admitted


# ---- library drafts (#1611) ---------------------------------------------------------------------
#
# What HQ drafted for a library card (a playbook's files, a skill's files, an automation's
# config) is far larger than a card record may hold, so it lives in ONE file per proposal,
# ``drafts/<cid>/<pid>.json``, written once through this boundary and never rewritten. Only its
# sha256 travels with the card: it is part of the card's keyed binding, so a draft changed on disk
# after staging makes the tap ``stale`` and nothing runs. It goes with its conversation.

#: The most a draft may hold, whatever the target store would take.
DRAFT_MAX_BYTES = 512 * 1024


def _drafts_dir(cid: str) -> Path:
    return root() / "drafts" / cid


def _draft_path(cid: str, pid: str) -> Path:
    return _drafts_dir(cid) / f"{pid}.json"


def _draft_texts(
    value: object, trusted: frozenset[tuple[str, ...]], at: tuple[str, ...] = ()
) -> Iterator[str]:
    """Every string a draft holds, keys included: each one goes through the boundary — except
    the value at a ``trusted`` PATH (e.g. ``("config", "action", "project_id")``), which the SERVER
    issued and which reads as credential-shaped only because it is random. A path, never a bare
    key name: a model-authored file called ``revision`` inside ``files`` is still guarded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            if (*at, k) not in trusted:
                yield from _draft_texts(v, trusted, (*at, str(k)))
    elif isinstance(value, list):
        for v in value:
            yield from _draft_texts(v, trusted, at)


def draft_digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def write_draft(
    cid: str, pid: str, content: dict, *, trusted: frozenset[tuple[str, ...]] = frozenset()
) -> str:
    """Store a NEW draft and return its sha256. Every text in it goes through the boundary
    WHOLE, before anything is cut: a text the boundary would change (a known secret, a
    credential-shaped value) refuses the whole draft (Withheld) — it is never stored altered.
    ``trusted`` names the PATHS whose values the server issued (never model text); they are skipped.
    Raises StoreRefused when redaction is unavailable or the draft is too large. Blocking."""
    if not valid_cid(cid) or not _UUID_HEX_RE.fullmatch(pid):
        raise ConversationNotFound(cid)
    data = json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(data) > DRAFT_MAX_BYTES:
        raise StoreRefused(f"the draft is larger than {DRAFT_MAX_BYTES // 1024} KiB")
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        for text in _draft_texts(content, trusted):
            if guard_text(text, max(len(text), 1), secrets=secrets) != text:
                raise Withheld("the draft contains a secret or looks like a credential")
        _ensure_root()
        _ensure_dir(root() / "drafts")
        d = _drafts_dir(cid)
        _ensure_dir(d)
        fd = os.open(
            _draft_path(cid, pid),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
    _sync_dir(d)
    return draft_digest(data)


def read_draft(cid: str, pid: str) -> tuple[dict, str]:
    """``(content, sha256 of the bytes read)``. Raises FileNotFoundError when there is none, and
    ValueError when it is not one bounded JSON object. Blocking."""
    if not valid_cid(cid) or not _UUID_HEX_RE.fullmatch(pid):
        raise FileNotFoundError(pid)
    fd = os.open(_draft_path(cid, pid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = os.read(fd, DRAFT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > DRAFT_MAX_BYTES:
        raise ValueError("draft too large")
    content = json.loads(data)
    if not isinstance(content, dict):
        raise ValueError("malformed draft")
    return content, draft_digest(data)


def _delete_drafts(cid: str) -> None:
    d = _drafts_dir(cid)
    with contextlib.suppress(FileNotFoundError):
        for name in os.listdir(d):
            (d / name).unlink(missing_ok=True)
        d.rmdir()


# ---- the follows sidecar (#1443 Phase 4) --------------------------------------------------------


@contextlib.contextmanager
def follows_lock(cid: str) -> Iterator[None]:
    """The follows sidecar's lock, blocking and short, across one read-modify-write."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_follows_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _empty_follows() -> dict:
    return {
        "follows": [],
        "seen_at": 0.0,
        "last_update_at": 0.0,
        "updates": 0,
        "consumed": [],
        "outbox": [],
    }


def read_follows(cid: str) -> dict:
    """The conversation's follows; an empty document when there is none. SidecarUnreadable when
    it exists and cannot be read — never a partial document."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        raw = _follows_path(cid).read_bytes()
    except FileNotFoundError:
        return _empty_follows()
    except OSError as e:
        raise SidecarUnreadable(type(e).__name__) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise SidecarUnreadable("not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("follows"), list):
        raise SidecarUnreadable("malformed")
    out = _empty_follows()
    out["follows"] = [f for f in data["follows"] if isinstance(f, dict)]
    for k in ("seen_at", "last_update_at"):
        v = data.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    n = data.get("updates")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        out["updates"] = n
    consumed = data.get("consumed") or []
    outbox = data.get("outbox") or []
    if not isinstance(consumed, list) or not isinstance(outbox, list):
        raise SidecarUnreadable("malformed")
    out["consumed"] = [c for c in consumed if isinstance(c, str)]
    out["outbox"] = [o for o in outbox if isinstance(o, dict)]
    return out


def _admit_follow(f: dict, secrets: list[str]) -> dict:
    key = f.get("key")
    if not (isinstance(key, str) and _KEY_RE.match(key)):
        raise ValueError("malformed followed session key")
    if f.get("via") not in FOLLOW_VIAS:
        raise ValueError("unknown follow origin")
    out: dict = {
        "key": key,
        "via": f["via"],
        "title": guard_text(f.get("title"), TITLE_MAX, secrets=secrets),
        "active": f.get("active") is True,
        "misses": f.get("misses") if isinstance(f.get("misses"), int) else 0,
    }
    v = f.get("needs_you")
    out["needs_you"] = v if isinstance(v, bool) else None
    # The newest visible-output time a `finished` has accounted for (#1443 Phase 4): a turn that
    # started AND ended between two passes still leaves a newer output time behind.
    t = f.get("out_through")
    out["out_through"] = (
        float(t) if isinstance(t, int | float) and not isinstance(t, bool) else None
    )
    since = f.get("since")
    out["since"] = float(since) if isinstance(since, int | float) else time.time()
    return out


def write_follows(cid: str, doc: dict) -> dict:
    """Replace the follows sidecar, atomically and durably, through the store boundary (titles are
    guarded). The CALLER holds :func:`follows_lock`. Returns the document as written."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    rows = doc.get("follows") or []
    if len(rows) > MAX_FOLLOW_ROWS:
        raise ValueError("too many follow rows")
    if sum(1 for f in rows if f.get("active") is True) > MAX_FOLLOWS:
        raise ValueError("too many active follows")
    _ensure_root()
    d = root() / "follows"
    _ensure_dir(d)
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        consumed = list(doc.get("consumed") or [])
        if len(consumed) > MAX_CONSUMED or not all(
            isinstance(c, str) and _ACTION_ID_RE.match(c) for c in consumed
        ):
            raise ValueError("malformed consumed ids")
        out = {
            "follows": [_admit_follow(f, secrets) for f in rows],
            "seen_at": float(doc.get("seen_at") or 0.0),
            "last_update_at": float(doc.get("last_update_at") or 0.0),
            "updates": int(doc.get("updates") or 0),
            "consumed": consumed,
            # Pending update records, admitted exactly as the conversation will store them.
            "outbox": [_admit(dict(o, type="update"), secrets) for o in doc.get("outbox") or []],
        }
        data = json.dumps(out, ensure_ascii=False).encode("utf-8", "replace")
        tmp = d / f".{cid}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, _follows_path(cid))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _sync_dir(d)
    return out


# ---- retention ---------------------------------------------------------------------------------


def _pinned_by_proposals(cid: str) -> bool:
    """A proposal mid-decision pins its conversation. An unreadable sidecar pins too: retention may
    not delete what recovery might still need."""
    path = _proposals_path(cid)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        data = json.loads(raw)
    except ValueError:
        return True
    rows = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return True
    return any(isinstance(p, dict) and p.get("state") in PINNING_PROPOSAL_STATES for p in rows)


def delete(cid: str) -> None:
    """Remove a conversation and everything keyed by its id. The sidecars go first and the JSONL
    last, so a crash part-way leaves the conversation listed and the next sweep finishes it."""
    _delete_drafts(cid)
    for p in (_proposals_path(cid), _follows_path(cid), _steps_path(cid)):
        p.unlink(missing_ok=True)
    _path(cid).unlink(missing_ok=True)
    from . import memory_capture

    memory_capture.invalidate_source(cid)
    for p in (
        _lock_path(cid),
        _decide_lock_path(cid),
        _proposals_lock_path(cid),
        _follows_lock_path(cid),
        _steps_lock_path(cid),
    ):
        p.unlink(missing_ok=True)
    # Shared files (#1707) go with the conversation, under their own fence — the same one an
    # upload holds across its publish, so this can never race a published orphan.
    from . import ask_attachments

    with contextlib.suppress(Exception):
        with ask_attachments.attachments_fence(cid):
            ask_attachments.delete_for_conversation(cid)


def _still_expired(cid: str, cutoff: float) -> bool:
    files = _conversation_files()
    for rank, (c, _p, mtime) in enumerate(files):
        if c == cid:
            return mtime < cutoff or rank >= MAX_CONVERSATIONS
    return False


class ConversationNotDeleted(Exception):
    """The conversation's own file could not be removed; nothing was removed."""


class ConversationBusy(Exception):
    """The operator asked to delete a conversation that cannot go yet: a turn is running in it, or
    a proposal in it is being decided (a launch whose record recovery may still need)."""


def delete_conversation(cid: str) -> None:
    """The operator deletes one conversation: the same locks and pins as retention's, decided
    under them. Raises ConversationNotFound, or ConversationBusy while a turn runs in it or a
    proposal in it is mid-decision. Blocking."""
    if not exists(cid):
        raise ConversationNotFound(cid)
    with turn_lock(cid) as free:
        if not free:
            raise ConversationBusy("HQ is still answering in this conversation")
        # Lock order: turn → proposals → follows, as retention takes them.
        with proposals_lock(cid), follows_lock(cid):
            if not exists(cid):
                raise ConversationNotFound(cid)
            if _pinned_by_proposals(cid):
                raise ConversationBusy("a session from this conversation is still starting")
            # The CONVERSATION goes first, in one unlink: until it is gone nothing else is
            # touched, so a failure here leaves it whole (its cards and follows intact). The unlink
            # is made DURABLE (the store directory fsynced) before any sidecar goes, so a power
            # loss can never bring the conversation back without its cards and follows. If the
            # sync fails, the sidecars are left: either the unlink survives and they are orphans,
            # or it doesn't and the conversation comes back whole. Orphans are removed by
            # retention's orphan pass (`sweep`), which takes no age into account. Retention's own
            # `delete` runs the other way round because an EXPIRED conversation left half-deleted
            # is still expired and the next sweep finishes it; a fresh one would not be.
            try:
                _path(cid).unlink()
            except FileNotFoundError:
                raise ConversationNotFound(cid) from None
            except OSError as e:
                raise ConversationNotDeleted(type(e).__name__) from None
            try:
                _sync_dir(root())
            except OSError:
                log.warning("ask conversation delete not yet durable; its sidecars stay for now")
                return
            from . import memory_capture

            memory_capture.invalidate_source(cid)
            for p in (
                _proposals_path(cid),
                _follows_path(cid),
                _steps_path(cid),
                _lock_path(cid),
                _decide_lock_path(cid),
                _proposals_lock_path(cid),
                _follows_lock_path(cid),
                _steps_lock_path(cid),
            ):
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
            # Its library drafts (#1611) go with it: authored content never outlives an explicit
            # delete. One that can't go now is an orphan the next sweep removes.
            with contextlib.suppress(OSError):
                _delete_drafts(cid)
            # And its shared files (#1707), under their fence: an upload holding that fence either
            # published before this (and goes now) or starts after and finds the conversation gone.
            from . import ask_attachments

            with contextlib.suppress(Exception):
                with ask_attachments.attachments_fence(cid):
                    ask_attachments.delete_for_conversation(cid)


def sweep(now: float | None = None) -> list[str]:
    """Apply retention. Returns the ids removed."""
    from . import memory_capture

    now = time.time() if now is None else now
    cutoff = now - RETENTION_DAYS * 86400
    removed: list[str] = []
    for n, (cid, _p, mtime) in enumerate(_conversation_files()):
        if mtime >= cutoff and n < MAX_CONVERSATIONS:
            continue
        if _pinned_by_proposals(cid) or memory_capture.retains_source(cid):
            continue
        with turn_lock(cid) as free:
            if not free:
                continue  # a turn is running in it
            # Decided AGAIN under the locks — every writer holds one — so a turn that settled or
            # a tap that started deciding while this waited is seen: fresh age and rank, and the
            # proposal pin read under the sidecar's own lock.
            # …and the follows lock, so a follower write that passed its own existence check is
            # finished before this deletes, never left behind after it. Lock order: turn →
            # proposals → follows.
            with proposals_lock(cid), follows_lock(cid):
                if (
                    not _still_expired(cid, cutoff)
                    or _pinned_by_proposals(cid)
                    or memory_capture.retains_source(cid)
                ):
                    continue
                delete(cid)
        removed.append(cid)
    # Sidecars whose conversation is gone (a crash between deletes, or an operator delete whose
    # cleanup didn't finish) follow it — but only once that ABSENCE is durable. Under the
    # sidecars' own locks (the order a delete takes them: proposals → follows), so this never
    # runs between a delete's unlink and its directory sync; the conversation's absence is
    # checked again there, and the store directory is synced before anything goes. A sync that
    # fails leaves the orphans for a later sweep: a crash could still bring the conversation
    # back, and it must come back whole (Hermes on #1564).
    for sub in ("proposals", "follows", "steps"):
        with contextlib.suppress(FileNotFoundError):
            for name in os.listdir(root() / sub):
                cid = name[: -len(".json")] if name.endswith(".json") else ""
                if not valid_cid(cid) or _path(cid).exists():
                    continue
                # Best effort, file by file: an orphan that can't go now is tried again at the
                # next sweep, and never fails the list or create request running this.
                try:
                    with proposals_lock(cid), follows_lock(cid):
                        if _path(cid).exists() or _pinned_by_proposals(cid):
                            continue
                        _sync_dir(root())
                        (root() / sub / name).unlink(missing_ok=True)
                        if sub == "follows":
                            _follows_lock_path(cid).unlink(missing_ok=True)
                        elif sub == "proposals":
                            _proposals_lock_path(cid).unlink(missing_ok=True)
                except OSError as e:
                    log.warning("ask orphan sidecar not removed yet (%s)", type(e).__name__)
    # Library drafts (#1611) whose conversation is gone, on the same terms.
    with contextlib.suppress(FileNotFoundError):
        for cid in os.listdir(root() / "drafts"):
            if not valid_cid(cid) or _path(cid).exists():
                continue
            try:
                with proposals_lock(cid), follows_lock(cid):
                    if _path(cid).exists():
                        continue
                    _sync_dir(root())
                    _delete_drafts(cid)
            except OSError as e:
                log.warning("ask orphan draft not removed yet (%s)", type(e).__name__)
    # Shared files (#1707): directories whose conversation is gone, and staged blobs past their
    # TTL that no user record claimed. Its own pass — the blobs are not sidecars.
    from . import ask_attachments

    with contextlib.suppress(Exception):
        ask_attachments.reap_staged(now)
    return removed
