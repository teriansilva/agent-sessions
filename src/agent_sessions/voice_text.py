"""Words for a call (#1397): the stored HQ answer as spoken text, and fixed card sentences.

No app imports: the voice worker imports this too, and the worker must not load the app.
Nothing here writes new content — it removes markdown from stored text, splits it for speech,
and renders FIXED sentences for what a turn left on screen (D8).
"""

from __future__ import annotations

import re

#: Bound on what one spoken answer may be (characters of stored text).
SPOKEN_MAX = 1_500

# --- what the microphone did NOT say (#1667) ----------------------------------------------------

#: Whisper's stock phrases on near-silence and non-speech sound: what room noise turned into on the
#: operator's first calls ("Thank you.", "好", "Tschüss.", "Obrigado."), plus the well-known
#: subtitle credits it learnt from. Only a WHOLE utterance is matched. Never listed: confirmation
#: or option words ("yes", "confirm", "stop", ordinals), and goodbyes ("bye", "tschüss", "ciao"):
#: whether those mean the call is over is HQ's judgement, so they must reach it.
NO_SPEECH_PHRASES = frozenset(
    {
        "thank you", "thank you very much", "thanks", "thanks for watching",
        "thank you for watching", "you", "so", "hmm", "mm", "uh", "um",
        "danke", "vielen dank", "danke schön",
        "obrigado", "obrigada", "gracias", "merci", "merci beaucoup", "grazie",
        "谢谢", "好", "嗯", "ご視聴ありがとうございました", "ありがとうございました",
    }
)  # fmt: skip
#: Whisper's subtitle-credit hallucinations, WHOLE utterances only, as they read after `_PUNCT`
#: (so "Amara.org" is "amara org"): a request that merely starts like one ("Subtitles by Friday,
#: please …") is never dropped (#1669 review).
_NO_SPEECH_CREDITS = re.compile(
    r"^(?:untertitel (?:im auftrag des zdf(?: für funk)?(?: \d{4})?|der amara org community"
    r"|von stephanie geiges)"
    r"|subtitles by the amara org community"
    r"|sous titres réalisés (?:par|para) la communauté d amara org"
    r"|sous titrage (?:st 501|société radio canada))$"
)
_PUNCT = re.compile(r"[\s.,!?;:…。、！？，「」\"'()\[\]-]+")


def is_no_speech_phrase(said: str) -> bool:
    """Whether an utterance is NOTHING but a stock phrase Whisper invents from noise (#1667)."""
    if not (said or "").strip():
        return False  # empty: not a phrase, and the caller already skips it
    text = " ".join(_PUNCT.sub(" ", said).split()).casefold()
    if not text:
        return True  # punctuation and nothing else ("."): no words were said
    return text in NO_SPEECH_PHRASES or bool(_NO_SPEECH_CREDITS.match(text))


#: How many consecutive words of HQ's own line make an echo (#1807). Three: HQ's shortest fixed
#: line ("One moment, Commander.") is three words, and two common words in a row ("the session")
#: are not evidence that the microphone heard the speaker.
ECHO_RUN_WORDS = 3


def _word(token: str) -> str:
    return _PUNCT.sub("", token).casefold()


def strip_echo(heard: str, said: list[str], *, run: int = ECHO_RUN_WORDS) -> str:
    """`heard` without every stretch of `run` or more consecutive words that is also a stretch of
    one of `said` — HQ's own lines, which the microphone picked up from the speaker (#1807: the
    operator's "Hey, what's up" arrived as "Hey, what's up One moment, Commander."). Words are
    compared without case or punctuation; what is kept is the operator's own text, untouched.
    An utterance that was nothing but echo comes back empty."""
    tokens = heard.split()
    words = [_word(t) for t in tokens]
    grams: set[tuple[str, ...]] = set()
    for line in said:
        w = [x for x in (_word(t) for t in line.split()) if x]
        grams.update(tuple(w[i : i + run]) for i in range(len(w) - run + 1))
    if not grams:
        return heard
    # Positions of the real words (punctuation-only tokens ride along with their neighbours).
    idx = [i for i, w in enumerate(words) if w]
    echoed: set[int] = set()
    for k in range(len(idx) - run + 1):
        span = idx[k : k + run]
        if tuple(words[i] for i in span) in grams:
            echoed.update(range(span[0], span[-1] + 1))
    kept = [t for i, t in enumerate(tokens) if i not in echoed]
    out = " ".join(kept).strip()
    return out if any(_word(t) for t in kept) else ""


def call_language(
    current: str | None, heard: str | None, text: str, listen: frozenset | None = None
) -> str | None:
    """The call's language after an utterance (#1397 P3): only a turn heard in one language, of
    3+ words, may change it — and never a stock no-speech phrase (#1667: "Thanks for watching"
    is three words of room noise), nor a language the call does not listen in (`listen`, #1807:
    that utterance is dropped by the app, so it must not move the language either — the next
    short "confirm" would otherwise be tagged with the noise's language)."""
    if listen and heard not in listen:
        return current
    if heard and len(text.split()) >= 3 and not is_no_speech_phrase(text):
        return heard
    return current


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


# --- deciding ONE Needs-you item by voice (#1681 P2) -------------------------------------------
#
# A permission dialog or a menu is offered ONE row per challenge, so a reply can only ever name
# something of THAT row. Same allowlist discipline as `classify_confirmation`: the whole
# utterance must be fillers and one phrase from these lists — nothing else, no question.

_ALLOW_PHRASES = frozenset({("allow",), ("allow", "it"), ("allow", "once"),
                            ("allow", "it", "once"), ("allow", "it", "just", "once"),
                            ("allow", "this", "once")})  # fmt: skip
_REFUSE_PHRASES = frozenset({("refuse",), ("refuse", "it"), ("deny",), ("deny", "it"),
                             ("reject",), ("reject", "it")})  # fmt: skip
#: What asks for MORE than one use: never a decision, and said back as such.
_BROADER = re.compile(r"\b(always|again|forever|session|everything|all|every)\b")
_SKIP_PHRASES = frozenset({("skip",), ("skip", "it"), ("skip", "this", "one"), ("next",),
                           ("next", "one")})  # fmt: skip
#: Words a menu pick may carry around its number: "pick the second one", "option two".
_PICK_WORDS = frozenset({"pick", "choose", "select", "take", "the", "option", "number", "please",
                         "ok", "okay", "yes", "yeah"})  # fmt: skip
_MENU_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
_MENU_CARDINALS = {"one": 0, "two": 1, "three": 2, "four": 3, "five": 4,
                   "1": 0, "2": 1, "3": 2, "4": 3, "5": 4}  # fmt: skip


def _words(said: str) -> tuple[str, list[str]]:
    raw = said.translate(_APOSTROPHES).lower()
    return raw, " ".join(re.sub(r"[^a-z0-9' ]", " ", raw).split()).split()


def is_skip(said: str) -> bool:
    """ "Skip" / "next": move on to the next item, leaving this one on the screen."""
    raw, words = _words(said)
    rest = tuple(w for w in words if w not in _FILLERS)
    return "?" not in raw and rest in _SKIP_PHRASES


def classify_decision(said: str, card: dict) -> tuple[str, dict | None]:
    """What a spoken reply decides about ONE permission or menu card (#1681 P2):

    - ``("choose", option)`` — the card's own recorded ``{"option", "label", "as"}``: its
      single-use allow for "allow it" (only when the card offers one), its refusal for "refuse
      it", or the menu option a bare number names ("the second one", "option two");
    - ``("skip", None)``, ``("cancel", None)``, ``("all", None)``;
    - ``("no_allow", None)`` — an allow phrase on a dialog that offers no single-use allow, and
      ``("broader", None)`` — an allow that asks for more than once ("allow always"): neither
      decides; HQ says why and the challenge re-arms;
    - ``("other", None)`` — anything else, a number past the card's options included: a new
      question, and the challenge is void.

    Never "confirm": a permission is answered by saying WHICH answer, and a broader grant
    ("allow always", "don't ask again") is not on the list, so it can never be said."""
    raw, words = _words(said)
    if not words:
        return "other", None
    if _CANCEL.match(" ".join(words)):
        return "cancel", None
    if "?" in raw or len(words) > CONFIRM_MAX_WORDS:
        return "other", None
    rest = tuple(w for w in words if w not in _FILLERS)
    if rest in _SKIP_PHRASES:
        return "skip", None
    if rest in _ALL_TAILS or (rest[:1] in (("refuse",), ("pick",), ("choose",))
                              and rest[1:] in _ALL_TAILS):  # fmt: skip
        return "all", None
    if card.get("kind") == "permission":
        if rest in _ALLOW_PHRASES:
            if isinstance(card.get("allow"), dict):
                return "choose", card["allow"]
            return "no_allow", None  # allow is not on offer for this dialog: say so
        if rest[:1] == ("allow",) and _BROADER.search(" ".join(rest)):
            return "broader", None  # "allow always" / "don't ask again": never by voice
        if rest in _REFUSE_PHRASES and isinstance(card.get("deny"), dict):
            return "choose", card["deny"]
        return "other", None
    if card.get("kind") in ("menu", "pick"):
        options = card.get("options") if isinstance(card.get("options"), list) else []
        pick = [w for w in words if w not in _PICK_WORDS]
        if len(pick) == 2 and pick[0] in _MENU_ORDINALS and pick[1] == "one":
            pick = pick[:1]  # "the second one"
        if len(pick) != 1:
            return "other", None
        if pick[0] in ("one", "1") and rest != ("one",) and not {"option", "number"} & set(rest):
            return "other", None  # "choose one" / "pick one" may mean "any": not option one
        i = _MENU_ORDINALS.get(pick[0], _MENU_CARDINALS.get(pick[0]))
        if i is None or i >= len(options):
            return "other", None  # not a number, or one the menu does not have
        return "choose", options[i]
    return "other", None


_ORDINAL_WORDS = ("first", "second", "third", "fourth")


def readback(cards: list[dict]) -> str:
    """The fixed read-back that arms a challenge: what each card does, then how to confirm."""
    if not cards:
        return ""
    if len(cards) == 1:
        c = cards[0]
        if c.get("ask"):  # a Needs-you item decided by its own words (#1681 P2)
            return f"{c['line']} {c['ask']}"
        return f"{c['line']} Say confirm to {c['verb']}, or cancel."
    parts = [f"{_ORDINAL_WORDS[i].capitalize()}: {c['line']}" for i, c in enumerate(cards)]
    return " ".join(parts) + " Say confirm and the number, or cancel."


def readback_screen_only(cards: list[dict]) -> str:
    """The read-back of cards that are NOT judged (#1715: Needs-you decisions, picks) on a call
    whose language the English matcher does not cover: what each card does, confirmed on the
    screen only. HQ cards are judged in any language and never read this way. (English: HQ
    answers in the call's language, but this fixed line stays short and plain.)"""
    if not cards:
        return ""
    parts = [c["line"] for c in cards]
    return " ".join(parts) + " Confirm it on your screen."
