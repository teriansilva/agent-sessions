"""Words for a call (#1397): the stored HQ answer as spoken text, and fixed card sentences.

No app imports: the voice worker imports this too, and the worker must not load the app.
Nothing here writes new content — it removes markdown from stored text, splits it for speech,
and renders FIXED sentences for what a turn left on screen (D8).
"""

from __future__ import annotations

import re

#: Bound on what one spoken answer may be (characters of stored text).
SPOKEN_MAX = 1_500

# --- spoken text -------------------------------------------------------------------------------

_MD = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), "a link"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*\d+[.)]\s+", re.M), ""),
    (re.compile(r"[*_~|>]+"), ""),
    (re.compile(r"\s+"), " "),
]


def speakable(text: str) -> str:
    """The stored answer as words to say: markdown punctuation removed, links not read out. The
    same words as the stored text, never new ones."""
    out = text or ""
    for pat, rep in _MD:
        out = pat.sub(rep, out)
    out = out.strip()
    if len(out) > SPOKEN_MAX:
        cut = out[:SPOKEN_MAX]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        out = (cut[: end + 1] if end > 200 else cut) + " The rest is on your screen."
    return out


#: A card's text is offered for a SPOKEN confirmation only if it can be read back in full.
READBACK_MAX = 300


def verbatim(text: str) -> str | None:
    """`text` as it will be read back, ONLY if that is the whole of it, word for word: plain prose
    (nothing `speakable` would drop or rewrite — no code, links or markup), at most READBACK_MAX
    characters. Otherwise None: what would be confirmed is not what was heard (#1631 review), so
    the card stays a screen decision."""
    plain = " ".join((text or "").split())
    if not plain or len(plain) > READBACK_MAX:
        return None
    if " ".join(speakable(plain).split()) != plain or any(c in plain for c in "`<>[]{}|*_#"):
        return None
    return plain


#: Words per spoken chunk: shorter chunks give earlier first audio on slow TTS servers.
CHUNK_WORDS = 14


def split_speech(text: str, *, first_max: int | None = None) -> list[str]:
    """Sentences, and long sentences split at commas/semicolons, each at most ~CHUNK_WORDS words
    where the text allows it; with `first_max`, the FIRST chunk is cut shorter still (at a comma
    if there is one, else at a word), so a slow TTS starts speaking sooner. Joining the chunks
    gives back the same words."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        if len(sentence.split()) <= CHUNK_WORDS:
            out.append(sentence)
            continue
        part: list[str] = []
        for clause in re.split(r"(?<=[,;:])\s+", sentence):
            if part and len(" ".join(part).split()) + len(clause.split()) > CHUNK_WORDS:
                out.append(" ".join(part))
                part = []
            part.append(clause)
        if part:
            out.append(" ".join(part))
    if first_max and out and len(out[0].split()) > first_max:
        words = out[0].split()
        cut = next(
            (i + 1 for i in range(min(first_max, len(words)) - 1, 2, -1) if words[i][-1] in ",;:"),
            first_max,
        )
        out[0:1] = [" ".join(words[:cut]), " ".join(words[cut:])]
    # A long stretch without punctuation is still cut, at words: no chunk over CHUNK_WORDS.
    fitted: list[str] = []
    for chunk in out:
        words = chunk.split()
        fitted += [" ".join(words[i : i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
    return [c for c in fitted if c]


def card_sentences(view: dict, *, skip: set[str] | frozenset = frozenset()) -> list[str]:
    """Fixed, server-rendered sentences for what an HQ turn left on screen (D8). The card text
    itself is not read out: the title and kind are enough to find it; the screen has the rest."""
    out: list[str] = []
    for p in view.get("proposals") or []:
        if p.get("action_id") in skip:
            continue  # read back as a confirmable card instead
        kind = p.get("kind")
        title = (p.get("title") or "").strip()[:80] or "that session"
        if kind == "nudge":
            out.append(f"I've drafted a message to {title}. It's on your screen to send.")
        elif kind == "session":
            out.append("I've drafted a new session. It's on your screen to start.")
        elif kind == "operation":
            label = (p.get("label") or p.get("verb") or "change").strip()[:60]
            out.append(f"I've drafted this: {label}, for {title}. Confirm it on your screen.")
    ask = view.get("ask")
    if isinstance(ask, dict) and ask.get("question"):
        opts = [o for o in ask.get("options") or [] if isinstance(o, str)]
        line = str(ask["question"]).strip()
        if opts:
            words = ("One", "Two", "Three", "Four")
            line += " " + " ".join(f"{words[i]}: {o}." for i, o in enumerate(opts[:4]))
        out.append(line)
    return out


_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3}
_CARDINALS = {"one": 0, "two": 1, "three": 2, "four": 3, "1": 0, "2": 1, "3": 2, "4": 3}


def pick_option(said: str, options: list[str]) -> int | None:
    """Which option a spoken reply names, or None. Exact option text wins; then "option two",
    "the second one", "two". Never a fuzzy guess between two options."""
    norm = re.sub(r"[^a-z0-9 ]", " ", said.lower()).split()
    joined = " ".join(norm)
    for i, opt in enumerate(options):
        o = " ".join(re.sub(r"[^a-z0-9 ]", " ", opt.lower()).split())
        if o and (joined == o or joined in (f"option {o}", f"the {o}")):
            return i
    # Only a bare pick counts: "two", "option two", "the second (one)". Anything else —
    # "No, not that one", "No. Just one." — is not an option number (#1626 review).
    rest = [w for w in norm if w not in ("option", "number", "the", "please")]
    if len(rest) == 2 and rest[0] in _ORDINALS and rest[1] == "one":
        rest = rest[:1]
    if len(rest) != 1:
        return None
    for table in (_ORDINALS, _CARDINALS):
        hits = {table[w] for w in rest if w in table and table[w] < len(options)}
        if len(hits) == 1:
            return hits.pop()
        if hits:
            return None
    return None


# --- spoken confirmation (#1397 P2) ------------------------------------------------------------

# An ALLOWLIST grammar (#1631 review): a blocklist of negations never converges ("can't",
# "won't", "should I send it", a curly apostrophe). An utterance confirms only if it is, word for
# word: fillers, ONE confirm phrase, and optionally the card's ordinal — nothing else, no question.
_CONFIRM_PHRASES = (
    ("confirm", "it"), ("confirm",), ("confirmed",), ("send", "it"), ("send",),
    ("start", "it"), ("start",), ("do", "it"), ("go", "ahead"), ("approve", "it"),
    ("approve",), ("approved",),
)  # fmt: skip
_FILLERS = frozenset({"please", "ok", "okay", "yes", "yeah"})
_SKIP = frozenset({"the", "number", "option", "please", "ok", "okay", "yes", "yeah"})
_CANCEL = re.compile(r"^(no|nope|cancel|cancel it|leave it|not now|never mind|dismiss)\b")
_ALL_TAILS = (("all",), ("everything",), ("both",), ("all", "of", "them"), ("them", "all"),
              ("every", "one"), ("each", "one"), ("all", "three"), ("all", "four"))  # fmt: skip
CONFIRM_MAX_WORDS = 6
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u0060": "'"})


def _confirm_rest(words: list[str]) -> list[str] | None:
    """The words after ONE leading confirm phrase (fillers before it skipped), or None."""
    i = 0
    while i < len(words) and words[i] in _FILLERS:
        i += 1
    for phrase in _CONFIRM_PHRASES:
        if tuple(words[i : i + len(phrase)]) == phrase:
            return words[i + len(phrase) :]
    return None


def classify_confirmation(said: str, cards: int) -> tuple[str, int | None]:
    """What a spoken reply to a read-back means, matched on the WHOLE utterance, never by a model:

    - ``("confirm", i)`` — fillers, one confirm phrase and, when more than one card is pending,
      exactly one ordinal naming card ``i``. Nothing else, and no question mark;
    - ``("cancel", None)`` — leave it (the card stays on screen, untouched);
    - ``("all", None)`` — a bulk request, refused;
    - ``("ambiguous", None)`` — a confirm without the ordinal several cards need;
    - ``("other", None)`` — anything else: a new question, and the challenge is void.
    """
    raw = said.translate(_APOSTROPHES).lower()
    norm = " ".join(re.sub(r"[^a-z0-9' ]", " ", raw).split())
    words = norm.split()
    if not words:
        return "other", None
    if _CANCEL.match(norm):
        return "cancel", None
    if "?" in raw or len(words) > CONFIRM_MAX_WORDS:
        return "other", None
    rest = _confirm_rest(words)
    if rest is None:
        return "other", None
    if tuple(w for w in rest if w not in _FILLERS) in _ALL_TAILS:
        return "all", None
    rest = [w for w in rest if w not in _SKIP]
    if len(rest) == 2 and rest[0] in _ORDINALS and rest[1] == "one":
        rest = rest[:1]  # "the second one"
    if not rest:
        picked = None
    elif len(rest) == 1 and (rest[0] in _ORDINALS or rest[0] in _CARDINALS):
        picked = _ORDINALS.get(rest[0], _CARDINALS.get(rest[0]))
    else:
        return "other", None  # any other word: "but", "later", "nothing", "that", …
    if picked is not None and picked >= cards:
        return "ambiguous", None  # names a card that was not read out
    if picked is None and cards != 1:
        return "ambiguous", None
    return "confirm", picked or 0


_ORDINAL_WORDS = ("first", "second", "third", "fourth")


def readback(cards: list[dict]) -> str:
    """The fixed read-back that arms a challenge: what each card does, then how to confirm."""
    if not cards:
        return ""
    if len(cards) == 1:
        c = cards[0]
        return f"{c['line']} Say confirm to {c['verb']}, or cancel."
    parts = [f"{_ORDINAL_WORDS[i].capitalize()}: {c['line']}" for i, c in enumerate(cards)]
    return " ".join(parts) + " Say confirm and the number, or cancel."


def readback_screen_only(cards: list[dict]) -> str:
    """The read-back on a call whose language the confirmation matcher does not cover: what each
    card does, confirmed on the screen only. (English: HQ answers in the call's language, but this
    fixed line stays short and plain.)"""
    if not cards:
        return ""
    parts = [c["line"] for c in cards]
    return " ".join(parts) + " Confirm it on your screen."
