"""AI-written templates (#1090, Phase 3) — "what template should I write?" and "write me one".

On request, and only on request, read the operator's OWN recent messages to their agents, find
what they keep retyping, and propose templates and library variables to write. Every suggestion is
a DRAFT: nothing is saved until the operator saves it in the editor or the variables form. There
is no loop and no background job.

**What leaves the process, and what does not.**

* Only user turns (what the operator typed), never agent output, from sessions whose cwd is inside
  the operator's hard boundary (roots + ``folder_exclusions`` — the terminal's rule, no curation),
  each TYPED in the last ``WINDOW_DAYS`` (the message's own time, not its session's; a turn the
  engine does not date is not sent), newest first, capped (``SESSIONS_MAX`` sessions,
  ``MESSAGES_MAX`` distinct messages, ``CHARS_MAX`` characters). Identical messages are sent once,
  with how many times and in how many sessions they were sent — which is what "repeats" means.
* Before anything is sent, every operator-authored string in the request — the messages and the
  existing templates' names and descriptions alike — has every stored and recently typed secret
  redacted (``template_secrets``; the transport redacts again). Then a string that shows ANY
  credential trigger (``credential_shaped``) is WITHHELD whole — never redacted and sent: a message
  is left out and counted, a description is sent empty, a template whose name trips it is not
  listed.
* The model's raw reply is never stored. Only validated suggestion fields are persisted; a draft
  with ANY text field that holds a stored secret, or that the value-aware draft net (``clean``)
  would change, is dropped. A variable the model marks ``secret`` carries NO value — the operator
  types it into the new-secret form.
* "Write me a template for …" (``write``) sends only the request and the names of the templates
  and library variables; a request that declares a credential is refused before it is sent, and the
  one draft is returned, never stored.

**Validated like anything the operator could have typed.** A template draft must pass
``templates.validate``; a variable name and text value must pass ``template_vars``' rules. Anything
that does not is dropped and counted, never repaired. A draft that duplicates an existing template
or variable name is dropped too. The result lives in ``template-suggestions.json`` (0600) with the
operator's dismissals, keyed by a content hash so a dismissed suggestion stays dismissed when a
later analysis proposes it again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

from . import (
    engines,
    metadata,
    prefs,
    project_dirs,
    prompts,
    review,
    template_secrets,
    template_vars,
    transcript,
)
from . import templates as tstore
from .atomicjson import atomic_write_json, json_write_lock

STORE_VERSION = 1
WINDOW_DAYS = 30
SESSIONS_MAX = 60
MESSAGES_MAX = 400
MESSAGE_CHARS_MAX = 1_500
CHARS_MAX = 60_000
SUGGESTIONS_MAX = 8
REASON_MAX = 300
DISMISSED_MAX = 500

REDACTED = "[redacted]"

# ---- the ad-hoc credential scrub ----------------------------------------------------------------

# Two different jobs, deliberately kept apart (#1110):
#
# * OUTBOUND — ``credential_shaped``. A message (or template description) that shows ANY credential
#   trigger is not sent at all. Redacting just the value needs to know where it ends — shell, JSON,
#   YAML and prose quoting — and six review rounds kept finding a new edge in that; detecting that a
#   trigger is PRESENT needs none of it. The cost is a message left out of an analysis that looks
#   for what repeats.
# * DRAFTS — ``scrub`` / ``clean``, a value-aware net over every field the model returns: a draft is
#   dropped if it would change. Value-aware only so a placeholder (``--password {{pw}}``) or a
#   secret-source read (``$(cat ~/.token)``) does not count; ordinary prose is not a trigger here.
#
# Every pattern is linear: no unbounded class inside another quantifier, and a trigger that starts
# inside a value an earlier trigger already consumed is skipped.

#: A key is a credential's name when one of its parts (split on ``_ . -`` and camelCase) is one of
#: these — anywhere in the key, so ``JWT_SECRET_KEY`` and ``GITHUB_TOKEN_2`` count — or when it
#: has a ``key`` part qualified by one of ``_KEY_QUALIFIERS`` (``api_key``, ``signing_key``).
_CRED_PARTS = frozenset(
    "password passwd passphrase pass pwd pw secret secrets token tokens apikey accesskey secretkey "
    "privatekey auth authorization bearer credential credentials cookie".split()
)
_KEY_QUALIFIERS = frozenset(
    "api access secret private signing master encryption client app license ssh".split()
)
_KEY_PART_RE = re.compile(r"[_.\-]+|(?<=[a-z0-9])(?=[A-Z])")
_WORD_RE = re.compile(r"[\w.-]+")
# `key: v`, `key = v`, `"key": v`, `'key' => v`, `\"key\": v` — never `==` or `=>` taken as a value.
_SEP_RE = re.compile(r"""\\*["'`]?[ \t]*(?:=>|[:=](?![=>]))[ \t]*""")
_NEXT_LINE_RE = re.compile(r"\n([ \t]+)(?=\S)")  # YAML: `password:` then the value, indented
_BLOCK_SCALAR_RE = re.compile(r"[|>][-+]?[ \t]*\n((?:[ \t]+[^\n]*(?:\n|$)|[ \t]*\n)+)")
_PROSE_WORD_RE = re.compile(r"[a-z]{1,12}")
_QUERY_NEXT_RE = re.compile(r"&(?=[\w.-]+=)")
_SCHEME_RE = re.compile(r"(?i)(?:bearer|basic|token|digest)[ \t]+(?=\S)")
_API_KEY_RE = re.compile(r"(?i)\bapi[ \t]+key[ \t]*[:=][ \t]*")
_QUOTES = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d", "\u2018": "\u2019"}
_OPEN_QUOTES = "".join(_QUOTES)
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,30}://)([^/\s:@]{0,256}):([^/\s]{1,512})@")
_AUTH_HEADER_RE = re.compile(
    r"(?i)\bauthorization[ \t]*:[ \t]*(?:(?:bearer|basic|token|digest)[ \t]+)?"
)
_BEARER_RE = re.compile(r"(?i)\bbearer[ \t]+(?=[A-Za-z0-9._~+/=-]{8})")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]{1,40}-----.*?(?:-----END [A-Z0-9 ]{1,40}-----|\Z)", re.S
)
_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
# Command-line and prose forms operators actually retype (independent review of #1110):
# `curl -u user:pass`, `--user user:pass`, `mysql -p'pw'` / `-ppw`, `sshpass -p pw`,
# `--password pw` / `--password=pw`, `docker login … -p pw`, `redis-cli -a pw`, and "the password
# is pw". A flag may start the text: messages are stripped before they are scrubbed.
_USERPASS_RE = re.compile(r"(?:^|(?<=\s))(?:-u|--user)(?:[ \t=]+|(?=\S))")
_QUOTED_P_RE = re.compile(r"(?:^|(?<=\s))-p(?=[\"'`\u201c\\])")
_VALUE_FLAG_RE = re.compile(
    r"(?i:\bsshpass)[ \t]+-p[ \t=]*(?=\S)"
    r"|(?:^|(?<=\s))--(?i:password|passwd|pass|secret|token|api-key|apikey)[ \t=]+(?=\S)"
)
#: A flag that is a password only in a given command's line: `mysql … -pX`, `docker login … -p X`,
#: `redis-cli … -a X`. Found by a forward scan of the rest of that line — no distance cutoff
#: (Hermes on #1110: a 300-character window let a long command's password through).
# A LOGICAL line: a backslash-newline continues the command (Hermes on #1110).
_LINE_RE = re.compile(r"(?:[^\n\\]|\\\r?\n|\\[^\n]?)+")
_CMD_FLAGS = (
    (re.compile(r"(?i)\b(?:mysql|mariadb|mysqldump|mysqladmin)\b"), re.compile(r"\s-p(?=\S)")),
    (re.compile(r"(?i)\bdocker[ \t]+login\b"), re.compile(r"\s-p[ \t=]*(?=\S)")),
    (re.compile(r"(?i)\bredis-cli\b"), re.compile(r"\s-a[ \t=]*(?=\S)")),
)
_PROSE_RE = re.compile(
    r"(?i)\b(?:password|passphrase|passwd|pass|pwd|pw|token|secret|api key)[ \t]+(?:is|was)"
    r"[ \t]*:?[ \t]+"
)
#: A value that is not a credential: a template placeholder, an UPPER_CASE or braced shell / CI
#: variable, a literal, or something already redacted. ``$hunter2`` and ``%hunter2%`` are NOT
#: variables — a password may start with ``$`` (independent review of #1110).
_INERT_RE = re.compile(
    r"\{\{[^{}\n]*\}\}|\$\{\{[^{}\n]*\}\}|\$\{[A-Za-z_]\w*\}|\$[A-Z_][A-Z0-9_]*|%[A-Z_][A-Z0-9_]*%"
    r"|<[^<>\n]*>|\*+|\[secret\]"
    # A read from a secret store or the environment — only in its exact shape, so a fallback
    # (`os.getenv("X", "hunter2")`) or an `$(echo hunter2)` is never taken for one.
    r"|\$\(\s*(?:cat|pass|gopass|op|vault|security|aws|gcloud|az|kubectl|jq)\s[^()\n\\\"'`]*\)"
    r"|process\.env\.[A-Z_][A-Z0-9_]*;?|os\.environ\[[\"'][A-Z_][A-Z0-9_]*[\"']\]"
    r"|os\.getenv\([\"'][A-Z_][A-Z0-9_]*[\"']\)"
    r"|(?i:true|false|null|none|yes|no|required|optional)"
)
_BARE_STRIP = "\\\"'`\u201c\u201d\u2018\u2019 \t"
ENTROPY_MIN = 3.5


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


_WORDLIKE_RE = re.compile(r"[A-Za-z]+\d{0,3}|\d{1,4}")


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _looks_random(run: str) -> bool:
    # A path, a long word or a snake/kebab-case name is not a credential: a run made only of
    # word-like pieces is left alone; otherwise require BOTH a mix of character classes and high
    # per-character entropy.
    if all(_WORDLIKE_RE.fullmatch(p) for p in re.split(r"[_/.=+-]+", run) if p):
        return False
    if _UUID_RE.search(run):
        # A session or request id is an identifier, not a secret: judge what is left around it.
        rest = [p for p in _UUID_RE.split(run) if len(p.strip("-_/.")) >= 20]
        return any(_looks_random(p.strip("-_/.")) for p in rest)
    classes = sum(bool(re.search(p, run)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[+/=_-]"))
    return classes >= 3 and _entropy(run) >= ENTROPY_MIN


#: Credential words that also count when run into another word (``PGPASSWORD``, ``ACCESSTOKEN``),
#: and endings that count at the end of one (``SSHPASS``, ``dbpass``, ``ROOTPW``).
_CRED_INFIXES = ("password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey")
_CRED_ENDINGS = ("pass", "pw", "pwd")


def _is_cred_key(word: str) -> bool:
    parts = {p.lower().rstrip("0123456789") for p in _KEY_PART_RE.split(word) if p}
    if parts & _CRED_PARTS or ("key" in parts and len(parts) > 1):
        return True
    return any(
        any(c in p for c in _CRED_INFIXES) or (len(p) > 4 and p.endswith(_CRED_ENDINGS))
        for p in parts
    )


def _value_end(text: str, i: int, stops: str = "", query: bool = False) -> int:
    """Where the value starting at ``i`` ends: one shell-like word. Quoted segments (and ``{{…}}`` /
    ``${{…}}`` expressions) are consumed whole and may be glued to bare text; a quote preceded by
    ``k`` backslashes closes only on a quote preceded by exactly ``k`` (so an escaped quote one JSON
    level down neither ends the value early nor is missed); a quoted value may span lines, and an
    unclosed quote runs to the end of the text. Bare text ends at whitespace or any of ``stops``,
    and so does a quote glued to the value and followed by a space — the close of an enclosing
    string, as in ``-H "X-Key: {{k}}" -H …``, not an opener running to the end of the text."""
    n = len(text)
    j = i
    while j < n:
        c = text[j]
        if c.isspace() or c in stops:
            break
        if query and c == "&" and _QUERY_NEXT_RE.match(text, j):
            break  # `?token=x&next=1` ends at the next query parameter; `hun&ter2` does not
        if c in "${" and text.startswith(("${{", "{{"), j):
            k = text.find("}}", j, j + 200)
            if k != -1 and "\n" not in text[j:k]:
                j = k + 2
                continue
        if c == "$" and text.startswith("$(", j):
            k = text.find(")", j, j + 300)  # a command substitution is one value
            if k != -1 and "\n" not in text[j:k]:
                j = k + 1
                continue
        if c == "\\" or c in _OPEN_QUOTES:
            k = j
            while k < n and text[k] == "\\":
                k += 1
            if k < n and text[k] in _OPEN_QUOTES:
                if j > i and (k + 1 == n or text[k + 1].isspace()):
                    break  # glued to the value and then a space: it CLOSES an enclosing string
                depth, close = k - j, _QUOTES[text[k]]
                m = k + 1
                while m < n:
                    if text[m] == close:
                        p = m - 1
                        while p > k and text[p] == "\\":
                            p -= 1
                        if m - 1 - p == depth:
                            m += 1
                            break
                    m += 1
                j = m
                continue
            j = max(k, j + 1)
            continue
        j += 1
    return j


def _bare(value: str) -> str:
    return value.strip(_BARE_STRIP)


def _inert(value: str) -> bool:
    v = _bare(value)
    return not v or v.startswith(REDACTED) or _INERT_RE.fullmatch(v) is not None


Span = tuple[int, int, str, str]  # start, end, replacement, the value removed


def _splice(text: str, spans: list[Span], removed: list[str]) -> str:
    """Apply non-overlapping edits, earliest first, recording each removed value."""
    out: list[str] = []
    pos = 0
    for a, b, rep, value in sorted(spans):
        if a < pos:
            continue
        out.append(text[pos:a])
        out.append(rep)
        removed.append(value)
        pos = b
    out.append(text[pos:])
    return "".join(out)


def _redact(text: str, a: int, b: int) -> Span | None:
    return None if b <= a or _inert(text[a:b]) else (a, b, REDACTED, text[a:b])


def _values_after(text: str, triggers: re.Pattern, stops: str = ""):
    consumed = 0
    for m in triggers.finditer(text):
        if m.start() < consumed:
            continue  # inside a value already taken — scanned once, not once per trigger
        consumed = _value_end(text, m.end(), stops)
        if span := _redact(text, m.end(), consumed):
            yield span


def _kv_values(text: str, spare_prose: bool = True):
    consumed = 0
    for m in _WORD_RE.finditer(text):
        if m.start() < consumed or not _is_cred_key(m.group(0)):
            continue
        sep = _SEP_RE.match(text, m.end())
        if sep is None:
            continue
        i = sep.end()
        colon = ":" in sep.group(0)
        if colon and (block := _BLOCK_SCALAR_RE.match(text, i)):
            consumed = block.end()  # YAML `password: |` — the value is the indented block below
            if span := _redact(text, block.start(1), consumed):
                yield span
            continue
        if (nl := _NEXT_LINE_RE.match(text, i)) and colon:
            i = nl.end()
        if scheme := _SCHEME_RE.match(text, i):
            i = scheme.end()  # `Authorization: Bearer x` — the credential is x, not the scheme
        consumed = _value_end(text, i, query=True)
        if (
            spare_prose
            and ":" in sep.group(0)
            and sep.group(0).endswith(" ")
            and _PROSE_WORD_RE.fullmatch(text[i:consumed])
        ):
            continue  # `Rotate the secret: update the vault` — a sentence, not a value
        if span := _redact(text, i, consumed):
            yield span


def _cmd_flag_starts(text: str):
    """Where each command-context password flag's value starts, one forward scan per line."""
    for line in _LINE_RE.finditer(text):
        for cmd, flag in _CMD_FLAGS:
            if c := cmd.search(text, line.start(), line.end()):
                for f in flag.finditer(text, c.end(), line.end()):
                    yield f.end()


def _cmd_flag_values(text: str):
    consumed = 0
    for i in sorted(set(_cmd_flag_starts(text))):
        if i < consumed:
            continue
        consumed = _value_end(text, i)
        if span := _redact(text, i, consumed):
            yield span


def _userpass_spans(text: str):
    consumed = 0
    for m in _USERPASS_RE.finditer(text):
        if m.start() < consumed:
            continue
        consumed = _value_end(text, m.end())
        user, colon, pw = text[m.end() : consumed].partition(":")
        if not colon or _inert(pw) or (_bare(user).isdigit() and _bare(pw).isdigit()):
            continue  # `-u name`, a placeholder, or a docker `uid:gid`
        yield m.end(), consumed, f"{_bare(user)}:{REDACTED}", pw


def _scrub(text: str, spare_prose: bool = True) -> tuple[str, list[str]]:
    removed: list[str] = []

    def sub(pattern: re.Pattern, fn) -> str:
        def rep(m: re.Match) -> str:
            out, value = fn(m)
            if value is not None:
                removed.append(value)
            return out

        return pattern.sub(rep, text)

    text = sub(_PEM_RE, lambda m: (REDACTED, m.group(0)))
    text = sub(
        _USERINFO_RE,
        lambda m: (m.group(0), None)
        if _inert(m.group(3))
        else (f"{m.group(1)}{m.group(2)}:{REDACTED}@", m.group(3)),
    )
    text = _splice(text, list(_values_after(text, _AUTH_HEADER_RE)), removed)
    text = _splice(text, list(_values_after(text, _BEARER_RE)), removed)
    text = _splice(text, list(_values_after(text, _API_KEY_RE)), removed)
    text = _splice(text, list(_kv_values(text, spare_prose)), removed)
    text = _splice(text, list(_userpass_spans(text)), removed)
    for triggers in (_QUOTED_P_RE, _VALUE_FLAG_RE):
        text = _splice(text, list(_values_after(text, triggers)), removed)
    text = _splice(text, list(_cmd_flag_values(text)), removed)
    text = sub(
        _RUN_RE,
        lambda m: (REDACTED, m.group(0)) if _looks_random(m.group(0)) else (m.group(0), None),
    )
    return text, removed


def scrub(text: str) -> str:
    """``text`` with credential-shaped values replaced by ``[redacted]``. Linear in the length of
    ``text``; a placeholder, variable or secret-source read is left alone."""
    return _scrub(text)[0]


def _joined(text: str) -> str:
    """``text`` as the shell reads it: a line continuation (backslash-newline) is REMOVED, not
    replaced — so ``doc\\⏎ker`` is ``docker`` and ``-\\⏎p`` is ``-p`` (Hermes on #1110). A
    recognition view only: a known-secret match always runs on the original bytes."""
    return _CONTINUATION_RE.sub("", text) if "\\" in text else text


_CONTINUATION_RE = re.compile(r"\\\r?\n")


def clean(text: str, secrets: list[str]) -> str:
    """The draft net: every stored/recent secret, then the credential-shaped values."""
    return scrub(template_secrets.redact_text(text, secrets))


def _net_catches(text: str, secrets: list[str]) -> bool:
    """Whether the draft net drops ``text``: it would change, or it declares a password."""
    # Known secrets on the ORIGINAL text (joining would change a secret that contains a
    # continuation); credential shapes on the shell's view of it.
    joined = _joined(text)
    return clean(text, secrets) != text or scrub(joined) != joined or _declares_value(text)


#: Outbound only: a credential word followed by something password-like (a digit or a symbol in
#: it), with no separator — `use password hunter2`, `password for staging is hunter2`, `pw x1`.
_WORD_THEN_VALUE_RE = re.compile(
    r"(?i)\b(?:password|passwd|passphrase|pwd|pw|pass|token|secret|api[ \t]?key)"
    r"(?:[ \t]+for[ \t]+\S{1,60})?(?:[ \t]+(?:is|was))?[ \t]+[`'\"]?"
    r"(?=\S{4})(?=\S{0,200}[\d!@#$%^&*+=?~])(?!\{\{)\S"
)
#: "the password is X" — a DECLARATION whatever X's complexity, unless X is an ordinary predicate
#: ("the password is rejected", "the token was expired", "the secret is stored hashed") or a
#: placeholder. Used by the write gate and the draft net; the outbound detector withholds any
#: "password is" at all.
_DECLARED_RE = re.compile(
    r"(?i)\b(?:password|passphrase|passwd|pwd|pw|token|secret|api[ \t]?key)"
    r"(?:[ \t]+for[ \t]+\S{1,60})?[ \t]+(?:is|was)[ \t]*:?[ \t]+(?:([`'\"\u201c])|(\S+))"
)
_PREDICATES = frozenset(
    """a an the not never always still now also only too being been very
    valid invalid wrong incorrect missing empty blank required optional set unset
    stored hashed encrypted salted logged shown hidden visible leaked exposed public private
    ok okay fine same different new old weak strong short long bad good fixed broken
    here there in on at from for with without used unused
    rejected expired revoked rotated reset changed checked validated verified compared accepted
    refused ignored sent passed read written cached saved deleted removed missing wrong""".split()
)


def _declares_value(text: str) -> bool:
    """Whether ``text`` declares a password: "the password is X". A QUOTED X is always a value —
    quoting is what makes it one (Hermes on #1110: "copperseed", "correct horse battery staple").
    An unquoted X is exempt only when it is a placeholder or one of a fixed list of instructional
    words ("rejected", "expired", "stored"); never by its spelling alone."""
    joined = _joined(text)
    for m in _DECLARED_RE.finditer(joined):
        if m.group(1):
            start = m.end() - 1
            end = _value_end(joined, start)
            if not _inert(joined[start:end]):
                return True
            continue
        word = m.group(2).strip("`'\".,;:!?\u201d")
        if word and not _inert(word) and word.lower() not in _PREDICATES:
            return True
    return False


#: A password piped into a command that reads it from stdin, and `htpasswd -b`.
_STDIN_SECRET_RE = re.compile(
    r"(?i)\becho\b[^\n|]{1,200}\|[^\n]{0,200}?(?:\bsudo[ \t]+-S\b|--password-stdin|\bchpasswd\b)"
    r"|\bhtpasswd[ \t]+-\w*b"
)

_TRIGGER_RES = (
    _PEM_RE,
    _USERINFO_RE,
    re.compile(r"(?i)\bauthorization[ \t]*:"),
    re.compile(r"(?i)\bbearer[ \t]+\S"),
    _API_KEY_RE,
    # `-u user:pw`, and the quoted form whose password may start with a space.
    re.compile(r"(?:^|\s)(?:-u|--user)[ \t=]*(?:[^\s:\"'`]*:\S|[\"'`][^\"'`\n:]*:[^\"'`\n]*\S)"),
    _QUOTED_P_RE,
    _VALUE_FLAG_RE,
    _PROSE_RE,
    _WORD_THEN_VALUE_RE,
    _STDIN_SECRET_RE,
)


def credential_shaped(text: str) -> bool:
    """Whether ``text`` shows ANY credential trigger — a credential-named key with a separator, a
    password flag, URL userinfo, an ``Authorization`` header, a private key, "the password is", or
    a long random-looking run — whatever follows it. Used to leave a message out, never to redact
    it, so it does not need to know where a value ends; a placeholder after a trigger counts too."""
    text = _joined(text)
    if any(r.search(text) for r in _TRIGGER_RES):
        return True
    if next(_cmd_flag_starts(text), None) is not None:
        return True
    for m in _WORD_RE.finditer(text):
        if _is_cred_key(m.group(0)) and _SEP_RE.match(text, m.end()):
            return True
    return any(_looks_random(m.group(0)) for m in _RUN_RE.finditer(text))


# ---- collection ---------------------------------------------------------------------------------


#: Text that arrives as a USER turn but was written by an agent (independent review of #1110):
#: Claude Code's context-compaction summary, and a handoff seed pasted into a new session (its
#: header, and its ``[agent]`` lines). "User turns only" means what the operator typed.
_AGENT_AUTHORED = (
    "This session is being continued from a previous conversation",
    "# Handoff",
    "Caveat: The messages below were generated by the user while running local commands",
)


def _usable(text: str) -> bool:
    t = text.strip()
    if len(t) < 12:
        return False
    if t.startswith(_AGENT_AUTHORED) or "\n[agent] " in t:
        return False
    # Harness/tool plumbing that lands in user turns, and slash commands (already a shortcut).
    return not (t.startswith("<") or t.startswith("/") or t.startswith("[Request interrupted"))


def collect(now: float | None = None) -> dict:
    """``collect_with_secrets`` without the redaction snapshot — the payload alone."""
    return collect_with_secrets(now)[0]


def collect_with_secrets(now: float | None = None) -> tuple[dict, list[str]]:
    """The operator's recent messages, scrubbed and de-duplicated. BLOCKING (walks transcripts).

    Returns ``{"messages": [{"text", "count", "sessions"}], "stats": {...}}``. Raises
    ``template_secrets.RedactionUnavailable`` if the secrets to redact cannot be established —
    nothing is collected for sending then."""
    now = time.time() if now is None else now
    since = now - WINDOW_DAYS * 86400
    roots, exclusions = project_dirs.effective_roots(), prefs.get_folder_exclusions()
    meta_index = metadata.load()

    def archived(r) -> bool:
        # The EFFECTIVE archive state, exactly as /api/sessions computes it: the sidecar wins
        # when set. opencode/codex archive by sidecar alone, so the raw scan flag is not enough
        # (independent review of #1110).
        key = f"{r.engine}:{r.uuid}"
        m = meta_index.get(key) or meta_index.get(engines.physical_key(key))
        return m.archived if m is not None and m.archived is not None else r.archived

    rows = [
        r
        for r in engines.scan_all_cached()
        if r.last_mtime >= since
        and project_dirs.in_scope(r.cwd, roots=roots, exclusions=exclusions)
        and not archived(r)
    ]
    rows.sort(key=lambda r: r.last_mtime, reverse=True)
    secrets = template_secrets.redaction_values()  # raises when it cannot be established
    # A session active in the window can hold turns typed long before it (a resumed session), so
    # the window is applied to each MESSAGE's own time, not the session's (Hermes on #1110). A turn
    # whose engine gave it no time cannot be shown to be inside the window, so it is not sent.
    typed: list[tuple[float, str, str]] = []
    scanned = withheld = 0
    for row in rows[:SESSIONS_MAX]:
        adapter = transcript.adapter_for(row.engine)
        if adapter is None:
            continue
        try:
            turns = adapter(row.uuid, Path.home())
        except Exception:  # noqa: BLE001, S112 — one unreadable transcript never sinks the analysis
            continue
        scanned += 1
        for t in turns:
            if t.kind != "text" or t.role != "user" or not _usable(t.text):
                continue
            if t.ts is None or t.ts < since:
                continue
            text = template_secrets.redact_text(t.text.strip(), secrets)
            if credential_shaped(text):
                withheld += 1  # never sent, not even redacted — see the scrub section
                continue
            text = text[:MESSAGE_CHARS_MAX]
            typed.append((t.ts, text, f"{row.engine}:{row.uuid}"))
    typed.sort(key=lambda e: e[0], reverse=True)  # newest first, across every session
    counts: Counter[str] = Counter()
    sessions: dict[str, set[str]] = {}
    order: list[str] = []
    for _ts, text, key in typed:
        if text not in counts:
            order.append(text)
        counts[text] += 1
        sessions.setdefault(text, set()).add(key)
    messages: list[dict] = []
    total = 0
    for text in order:  # most recently typed first, so the cap keeps recent habits
        if len(messages) >= MESSAGES_MAX or total + len(text) > CHARS_MAX:
            break
        messages.append({"text": text, "count": counts[text], "sessions": len(sessions[text])})
        total += len(text)
    # The redaction set this collection used travels BESIDE the payload, never inside it (a payload
    # is what gets serialised and sent); validation reuses it rather than re-deriving one after the
    # model call, where a failure would mis-report "nothing was sent".
    payload = {
        "messages": messages,
        "stats": {
            "messages": sum(m["count"] for m in messages),
            "distinct": len(messages),
            "sessions": scanned,
            "withheld": withheld,
            "days": WINDOW_DAYS,
        },
    }
    return payload, secrets


# ---- validation ---------------------------------------------------------------------------------


def _sid(kind: str, name: str, content: str) -> str:
    return hashlib.sha256(f"{kind}\0{name}\0{content}".encode()).hexdigest()[:16]


def _count(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < 10**6 else 0


def _reason(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:REASON_MAX]


def _texts(sug: dict):
    """Every string a suggestion stores and returns — each one is checked, not only the value."""
    for key in ("name", "reason", "body", "value"):
        if isinstance(sug.get(key), str):
            yield sug[key]
    for f in sug.get("fields", []):
        yield from (f["name"], f["label"], f["default"])


def validate_suggestions(
    obj: object,
    *,
    taken_templates: set[str],
    taken_vars: set[str],
    secrets: list[str] | None = None,
) -> tuple[list[dict], int]:
    """``(suggestions, dropped)``. Every draft is validated by the SAME rules a save would apply;
    anything that fails is dropped and counted, never repaired."""
    raw = obj.get("suggestions") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return [], 0
    out: list[dict] = []
    dropped = 0
    seen: set[str] = set()
    for item in raw[: SUGGESTIONS_MAX * 2]:
        if len(out) >= SUGGESTIONS_MAX:
            break
        try:
            if not isinstance(item, dict):
                raise ValueError
            kind = item.get("kind")
            if kind == "template":
                fields = item.get("fields") if isinstance(item.get("fields"), list) else []
                draft = tstore.validate(
                    {
                        "name": item.get("name"),
                        "description": _reason(item.get("reason"))[: tstore.DESCRIPTION_MAX],
                        "tags": [],
                        "body": item.get("body"),
                        "fields": [
                            {
                                "name": f.get("name"),
                                "label": f.get("label") or "",
                                "default": f.get("default") or "",
                            }
                            for f in fields
                            if isinstance(f, dict)
                        ],
                        "images": [],
                    },
                    check_files=False,
                )
                if draft["name"].lower() in taken_templates:
                    raise ValueError
                sug = {
                    "kind": "template",
                    "name": draft["name"],
                    "reason": _reason(item.get("reason")),
                    "count": _count(item.get("count")),
                    "body": draft["body"],
                    "fields": [
                        {"name": f["name"], "label": f["label"], "default": f["default"]}
                        for f in draft["fields"]
                    ],
                }
                sug["id"] = _sid("template", draft["name"], draft["body"])
            elif kind == "variable":
                secret = item.get("secret") is True
                name = item.get("name")
                if secret:
                    # A credential never rides a suggestion: the name is validated, the value
                    # (whatever the model put there) is discarded unread.
                    template_vars.validate({"name": name, "value": "x" * 8, "kind": "secret"})
                    value = ""
                else:
                    value = template_vars.validate({"name": name, "value": item.get("value")})[
                        "value"
                    ]
                if name in taken_vars:
                    raise ValueError
                sug = {
                    "kind": "variable",
                    "name": name,
                    "reason": _reason(item.get("reason")),
                    "count": _count(item.get("count")),
                    "value": value,
                    "secret": secret,
                }
                sug["id"] = _sid("variable", name, "secret" if secret else value)
            else:
                raise ValueError
            # No stored text field may hold a secret, a value the scrub removed before the call,
            # or anything the scrub would remove now. The last is the net for what the outbound
            # scrub MISSED: that reached the model unchanged, and it can come back verbatim or
            # reshaped into a form the scrub does catch (independent review of #1110). Its cost is
            # an occasional good draft dropped, never a credential kept.
            if any(_net_catches(t, secrets or []) for t in _texts(sug)):
                raise ValueError
            # Only an ACCEPTED draft reserves its name: a dropped one must not take a later,
            # valid draft of the same name down with it (independent review of #1110).
            if sug["kind"] == "template":
                taken_templates = taken_templates | {sug["name"].lower()}
            else:
                taken_vars = taken_vars | {sug["name"]}
        except (ValueError, tstore.TemplateError, template_vars.VariableError):
            dropped += 1
            continue
        if sug["id"] in seen:
            continue
        seen.add(sug["id"])
        out.append(sug)
    return out, dropped


# ---- store --------------------------------------------------------------------------------------


def store_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_TEMPLATE_SUGGESTIONS",
            str(tstore.store_path().with_name("template-suggestions.json")),
        )
    )


class StoreUnreadable(RuntimeError):
    """The suggestions file exists but cannot be read as this version's document."""


def _read() -> dict:
    """Lenient, for DISPLAY: anything unreadable shows as never analysed."""
    try:
        return _read_checked()
    except StoreUnreadable:
        return {}


def _read_checked() -> dict:
    """Strict, for a WRITE: only a file that does not exist is empty (Hermes on #1110). An
    unreadable, malformed or other-version file raises, because rewriting it from ``{}`` would
    erase the previous result and every dismissal."""
    try:
        raw = store_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # ValueError: not UTF-8
        raise StoreUnreadable(
            f"the suggestions file cannot be read ({e.__class__.__name__})"
        ) from e
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise StoreUnreadable("the suggestions file is not valid JSON") from e
    if not isinstance(doc, dict) or doc.get("version") != STORE_VERSION:
        raise StoreUnreadable("the suggestions file is not a version this app writes")
    return doc


def _dismissed(doc: dict) -> list[str]:
    raw = doc.get("dismissed")
    return [d for d in raw if isinstance(d, str)][-DISMISSED_MAX:] if isinstance(raw, list) else []


def current() -> dict | None:
    """The last analysis, minus what the operator dismissed — or ``None`` if never analysed."""
    doc = _read()
    if "suggestions" not in doc or not isinstance(doc["suggestions"], list):
        return None
    dismissed = set(_dismissed(doc))
    # A draft the operator has since SAVED is no longer a suggestion (independent review of #1110).
    taken_templates = {t["name"].lower() for t in tstore.list_templates()}
    taken_vars = set(template_vars.values()) | set(template_vars.secret_state())

    def still_open(s: dict) -> bool:
        if s.get("kind") == "template":
            return str(s.get("name", "")).lower() not in taken_templates
        return s.get("name") not in taken_vars

    return {
        "generated_at": doc.get("generated_at"),
        "stats": doc.get("stats") or {},
        "dropped": doc.get("dropped", 0),
        "suggestions": [
            s
            for s in doc["suggestions"]
            if isinstance(s, dict) and s.get("id") not in dismissed and still_open(s)
        ],
    }


def _write(update) -> None:
    path = store_path()
    with json_write_lock(path):
        doc = _read_checked()
        update(doc)
        doc["version"] = STORE_VERSION
        atomic_write_json(path, doc, mode=0o600)


def dismiss(sid: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        raise ValueError("unknown suggestion")

    def update(doc: dict) -> None:
        dismissed = _dismissed(doc)
        if sid not in dismissed:
            dismissed.append(sid)
        doc["dismissed"] = dismissed[-DISMISSED_MAX:]

    _write(update)


# ---- the analysis -------------------------------------------------------------------------------

# Single-flight across event loops (a module-level asyncio.Lock binds to the first loop it sees).
_running = False
_running_lock = threading.Lock()


class AlreadyRunning(RuntimeError):
    pass


async def analyse() -> dict:
    """Collect, ask, validate, persist. Raises ``AlreadyRunning``, ``review.NotConfiguredError``,
    ``review.ReviewError``, ``template_secrets.RedactionUnavailable`` or ``StoreUnreadable``. A
    failed analysis keeps the previous result — only a SUCCESS replaces it."""
    global _running
    with _running_lock:
        if _running:
            raise AlreadyRunning("an analysis is already running")
        _running = True
    try:
        return await _analyse()
    finally:
        with _running_lock:
            _running = False


def _sendable(text: str, secrets: list[str]) -> str | None:
    """``text`` with stored secrets redacted, or ``None`` if it shows a credential trigger."""
    text = template_secrets.redact_text(text, secrets)
    return None if credential_shaped(text) else text


def _metadata_out(existing: list[dict], secrets: list[str]) -> list[dict]:
    """The existing templates as the model sees them — operator-authored text, so under the same
    rule as the messages (Hermes on #1110): a description that shows a credential trigger is sent
    empty, and a template whose NAME shows one is not listed (duplicates are still refused here)."""
    out = []
    for t in existing:
        name = _sendable(t["name"], secrets)
        if name is not None:
            out.append({"name": name, "description": _sendable(t["description"], secrets) or ""})
    return out


async def _analyse() -> dict:
    review._require_config()  # fail fast, before any transcript is read
    await asyncio.to_thread(_read_checked)  # and before anything is sent that could not be kept
    collected, secrets = await asyncio.to_thread(collect_with_secrets)
    existing = await asyncio.to_thread(tstore.list_templates)
    variables = await asyncio.to_thread(template_vars.list_variables)
    taken_templates = {t["name"].lower() for t in existing}
    taken_vars = {v["name"] for v in variables}
    if not collected["messages"]:
        result = {"suggestions": [], "dropped": 0}
    else:
        # Operator-authored too, so scrubbed like the messages (Hermes on #1110): a description can
        # hold a hand-pasted credential. The scrubs run in a worker thread, like the collection —
        # never on the event loop every terminal socket shares.
        metadata_out = await asyncio.to_thread(_metadata_out, existing, secrets)
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("template_suggest")},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "messages": collected["messages"],
                            "existing_templates": metadata_out,
                            "existing_variables": sorted(taken_vars),
                        }
                    ),
                },
            ]
        )
        sugs, dropped = await asyncio.to_thread(
            validate_suggestions,
            obj,
            taken_templates=taken_templates,
            taken_vars=taken_vars,
            secrets=secrets,
        )
        result = {"suggestions": sugs, "dropped": dropped}
    generated = time.time()

    def update(doc: dict) -> None:
        doc["generated_at"] = generated
        doc["stats"] = collected["stats"]
        doc["dropped"] = result["dropped"]
        doc["suggestions"] = result["suggestions"]  # validated fields only, never raw

    await asyncio.to_thread(_write, update)
    out = current()
    assert out is not None
    return out


# ---- "write me a template for …" -----------------------------------------------------------------
#
# The operator describes a template and the model drafts ONE. Only the request is sent, with the
# names of the existing templates (under the metadata rule above) and of the library variables so
# the draft can use them — never a transcript. A request that shows a credential trigger is refused
# before anything is sent. The draft is returned, never stored: it opens in the editor, and nothing
# is saved until the operator saves it there.

REQUEST_MAX = 2_000


class RequestRefused(ValueError):
    """The request cannot be sent as it is; the message says why. Nothing was sent."""


class DraftInvalid(ValueError):
    """The model's reply is not a template this app would save."""


_writing = False
_writing_lock = threading.Lock()


def validate_draft(obj: object, *, library: dict[str, bool], secrets: list[str]) -> dict:
    """The model's draft, validated by the SAME rules a save applies, under the same draft net as
    the suggestions. A slot named after a library variable becomes a library field; a slot the
    model marks secret becomes a secret field with no default."""
    if not isinstance(obj, dict):
        raise DraftInvalid("the reply is not a JSON object")
    raw_fields = obj.get("fields") if isinstance(obj.get("fields"), list) else []
    fields = []
    for f in raw_fields:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not isinstance(name, str):
            raise DraftInvalid("a field name is not text")
        label = f.get("label") if isinstance(f.get("label"), str) else ""
        if name in library:
            source, kind, default = "library", "secret" if library[name] else "text", ""
        else:
            secret = f.get("secret") is True
            source, kind = "template", "secret" if secret else "text"
            default = "" if secret or not isinstance(f.get("default"), str) else f["default"]
        fields.append(
            {
                "name": name,
                "label": label,
                "default": default,
                "required": False,
                "source": source,
                "kind": kind,
            }
        )
    description = obj.get("description") if isinstance(obj.get("description"), str) else ""
    try:
        draft = tstore.validate(
            {
                "name": obj.get("name"),
                "description": _reason(description)[: tstore.DESCRIPTION_MAX],
                "tags": [],
                "body": obj.get("body"),
                "fields": fields,
                "images": [],
            },
            check_files=False,
        )
    except tstore.TemplateError as e:
        raise DraftInvalid(str(e)) from None
    out = {
        "name": draft["name"],
        "description": draft["description"],
        "body": draft["body"],
        "fields": [
            {k: f[k] for k in ("name", "label", "default", "source", "kind")}
            for f in draft["fields"]
        ],
    }
    texts = [out["name"], out["description"], out["body"]]
    texts += [t for f in out["fields"] for t in (f["name"], f["label"], f["default"])]
    if any(_net_catches(t, secrets) for t in texts):
        raise DraftInvalid("the draft contains something shaped like a credential")
    return out


async def write(request: object) -> dict:
    """Draft one template for ``request``. Raises ``RequestRefused`` (nothing sent),
    ``AlreadyRunning``, ``review.NotConfiguredError``, ``review.ReviewError``,
    ``template_secrets.RedactionUnavailable`` or ``DraftInvalid``."""
    global _writing
    if not isinstance(request, str) or not request.strip():
        raise RequestRefused("Describe the template you want.")
    request = request.strip()
    if len(request) > REQUEST_MAX:
        raise RequestRefused(f"Keep the request under {REQUEST_MAX} characters.")
    review._require_config()
    with _writing_lock:
        if _writing:
            raise AlreadyRunning("a template is already being written")
        _writing = True
    try:
        secrets = await asyncio.to_thread(template_secrets.redaction_values)
        sendable = template_secrets.redact_text(request, secrets)
        # The request is typed to BE sent, so it is judged by value, not by trigger: "adding
        # bearer auth" or "--password {{pw}}" is a description; `password = correcthorse` and
        # "the password is hunter2" are not (Hermes on #1110) — no prose word is spared here.
        joined = _joined(sendable)
        if (
            _scrub(joined, spare_prose=False)[0] != joined
            or _WORD_THEN_VALUE_RE.search(joined)
            or _declares_value(joined)
            or _STDIN_SECRET_RE.search(joined)
        ):
            raise RequestRefused(
                "Your request looks like it contains a password or token. Describe it with a "
                "{{placeholder}} instead — nothing was sent."
            )
        existing = await asyncio.to_thread(tstore.list_templates)
        variables = await asyncio.to_thread(template_vars.list_variables)
        library = {v["name"]: v.get("kind") == "secret" for v in variables}
        names = await asyncio.to_thread(
            lambda: [n for t in existing if (n := _sendable(t["name"], secrets)) is not None]
        )
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("template_write")},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": sendable,
                            "existing_templates": names,
                            "library_variables": sorted(library),
                        }
                    ),
                },
            ]
        )
        return await asyncio.to_thread(validate_draft, obj, library=library, secrets=secrets)
    finally:
        with _writing_lock:
            _writing = False
