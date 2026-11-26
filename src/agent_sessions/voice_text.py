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


#: What stops HQ's running turn when it is the WHOLE utterance (#2076), by language. Only while
#: a turn runs (the caller's rule); at any other time these words reach HQ like any others. Never
#: a goodbye: stopping the work never ends the call.
STOP_PHRASES: dict[str, frozenset[str]] = {
    "en": frozenset({
        "stop", "stop it", "stop that", "please stop", "stop please", "cancel", "cancel that",
        "cancel it", "never mind", "nevermind",
    }),
    "de": frozenset({
        "stopp", "stop", "halt", "halt stopp", "abbrechen", "abbruch", "bitte abbrechen",
        "vergiss es", "hör auf",
    }),
    "es": frozenset({"para", "detente", "alto", "cancela", "cancelar", "olvídalo"}),
    "fr": frozenset({"stop", "arrête", "arrêtez", "annule", "annuler", "laisse tomber"}),
    "it": frozenset({"stop", "ferma", "fermati", "basta", "annulla", "lascia stare"}),
    "pt": frozenset({"pare", "para", "cancela", "cancelar", "esquece", "deixa para lá"}),
}  # fmt: skip


#: What stops EVERYTHING when it is the WHOLE utterance (#2103): every running HQ task, and
#: every question waiting behind one. Same rule and same languages as `STOP_PHRASES`: a sentence
#: that merely contains one is a request. Checked before the plain stop.
STOP_ALL_PHRASES: dict[str, frozenset[str]] = {
    "en": frozenset({
        "stop everything", "stop all", "stop it all", "stop all of it", "cancel everything",
        "cancel all", "please stop everything",
    }),
    "de": frozenset({
        "alles stoppen", "stopp alles", "stop alles", "alles anhalten", "alles abbrechen",
        "brich alles ab", "hör mit allem auf",
    }),
    "es": frozenset({"para todo", "detén todo", "cancela todo", "cancelar todo"}),
    "fr": frozenset({"arrête tout", "arrêtez tout", "annule tout", "stop tout"}),
    "it": frozenset({"ferma tutto", "annulla tutto", "stop tutto", "basta tutto"}),
    "pt": frozenset({"pare tudo", "para tudo", "cancela tudo", "cancelar tudo"}),
}  # fmt: skip


#: Words that LOOK like a plain goodbye (2026-11-25: "Okay, thank you, General. Bye-bye." took
#: 36 s through a whole HQ turn). The WHOLE utterance must be made of these words and hold at
#: least one farewell (`GOODBYE_CORE`, or "see you"). They decide nothing: they only send the
#: words to HQ's quick call-end judgement (`voice_judge.judge_goodbye`) instead of a whole turn;
#: whether the call is over stays HQ's call, never a word list's (operator, 2026-10-24).
GOODBYE_CORE: dict[str, frozenset[str]] = {
    "en": frozenset({"bye", "goodbye", "byebye", "cya", "farewell"}),
    "de": frozenset({"tschüss", "tschüs", "tschuss", "tschau", "tchau", "ciao", "wiedersehen"}),
}  # fmt: skip
GOODBYE_FILLER: dict[str, frozenset[str]] = {
    "en": frozenset({
        "ok", "okay", "alright", "right", "well", "so", "then", "thanks", "thank", "you", "very",
        "much", "a", "lot", "cheers", "take", "care", "have", "nice", "good", "great", "day",
        "evening", "night", "later", "soon", "talk", "to", "see", "general", "hq", "commander",
        "sir", "for", "now", "thats", "it", "all",
    }),
    "de": frozenset({
        "ok", "okay", "also", "gut", "na", "dann", "danke", "dankeschön", "schön", "sehr",
        "vielen", "dank", "bis", "später", "bald", "auf", "einen", "schönen", "tag", "abend",
        "gute", "nacht", "machs", "general", "das", "wars", "erstmal",
    }),
}  # fmt: skip
GOODBYE_MAX_WORDS = 12


def is_goodbye(
    said: str, languages: frozenset | None = None, names: frozenset[str] = frozenset()
) -> bool:
    """Whether an utterance is NOTHING but a goodbye, in one of ``languages`` (as
    `is_stop_phrase`). ``names``: the contact's own names, which a goodbye may hold ("Bye,
    General Harlan.")."""
    words = [_word(w) for w in re.split(r"[\s\-]+", said or "")]
    words = [w for w in words if w]
    if not words or len(words) > GOODBYE_MAX_WORDS:
        return False
    langs = [lang for lang in GOODBYE_CORE if not languages or lang in languages] or list(
        GOODBYE_CORE
    )
    core = frozenset().union(*(GOODBYE_CORE[lang] for lang in langs))
    allowed = core | names | frozenset().union(*(GOODBYE_FILLER[lang] for lang in langs))
    if not all(w in allowed for w in words):
        return False
    see_you = "en" in langs and any(
        a == "see" and b == "you" for a, b in zip(words, words[1:], strict=False)
    )
    return see_you or any(w in core for w in words)


def _whole_phrase(said: str, phrases: dict, languages: frozenset | None) -> bool:
    text = " ".join(_PUNCT.sub(" ", said or "").split()).casefold()
    if not text:
        return False
    langs = [lang for lang in phrases if not languages or lang in languages]
    return any(text in phrases[lang] for lang in langs or phrases)


def is_stop_all_phrase(said: str, languages: frozenset | None = None) -> bool:
    """Whether an utterance is NOTHING but a stop-everything phrase (#2103), in one of
    ``languages`` (as `is_stop_phrase`)."""
    return _whole_phrase(said, STOP_ALL_PHRASES, languages)


def is_stop_phrase(said: str, languages: frozenset | None = None) -> bool:
    """Whether an utterance is NOTHING but a stop phrase (#2076), in one of ``languages`` (the
    call's listening set; ``None`` or empty: every language listed). A sentence that merely
    contains one ("stop the staging deploy") is a request, never a stop."""
    return _whole_phrase(said, STOP_PHRASES, languages)


#: How many consecutive words of HQ's own line make an echo (#1807). Three: HQ's shortest fixed
#: line ("Take care, Commander.") is three words, and two common words in a row ("the session")
#: are not evidence that the microphone heard the speaker.
ECHO_RUN_WORDS = 3
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")


def _word(token: str) -> str:
    return _PUNCT.sub("", token).casefold()


def strip_echo(heard: str, said: list[str], *, run: int = ECHO_RUN_WORDS) -> str:
    """`heard` without every stretch of `run` or more consecutive words that is also a stretch of
    one of `said` — HQ's own lines, which the microphone picked up from the speaker (#1807: the
    operator's "Hey, what's up" arrived as "Hey, what's up" plus HQ's line). Words are
    compared without case or punctuation; what is kept is the operator's own text, untouched.
    An utterance that was nothing but echo comes back empty.

    A short question HQ asks ("Which folder?", #2342) is under `run` words, so no stretch can
    catch it: what remains is ALSO dropped when it is, word for word and whole, one QUESTION
    sentence of a line in `said`. Only the whole remainder: a reply that merely contains it is
    kept, and a short statement ("Yes.") is never matched this way."""
    tokens = heard.split()
    words = [_word(t) for t in tokens]
    grams: set[tuple[str, ...]] = set()
    short: set[tuple[str, ...]] = set()
    for line in said:
        w = [x for x in (_word(t) for t in line.split()) if x]
        grams.update(tuple(w[i : i + run]) for i in range(len(w) - run + 1))
        for sentence in _SENTENCE_END.split(line):
            sw = tuple(x for x in (_word(t) for t in sentence.split()) if x)
            # Questions only: a short statement ("Yes." inside a message read back word for word)
            # may be exactly what the Commander answers, a question of HQ's hardly ever is.
            if 0 < len(sw) < run and sentence.rstrip().endswith("?"):
                short.add(sw)
    if not grams and not short:
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
    if tuple(w for w in (_word(t) for t in kept) if w) in short:
        return ""  # nothing but one of HQ's short questions, heard back
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
        out = (cut[: end + 1] if end > 200 else cut) + " The rest is in the chat."
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
#: For a TTS server that STREAMS (`whole`): whole sentences, together up to this many words per
#: request. Such a server starts speaking a fraction of a second into any request, so nothing is
#: gained by cutting — and each request is voiced on its own: measured on VoxCPM2 (2026-11-13, 432
#: renders), a piece of one to five words left its voice's pitch in 12 of 72 renders, a sentence
#: of six or more in none. Cut pieces are what made HQ sound calm, then suddenly excited.
WHOLE_WORDS = 40
#: A last piece shorter than this joins the one before it rather than being voiced alone, even
#: past WHOLE_WORDS: a request is at most WHOLE_WORDS + WHOLE_MIN_WORDS - 1 words.
WHOLE_MIN_WORDS = 6


def _length(chunk: str) -> int:
    return len(chunk.split())


def _whole_sentences(pieces: list[str]) -> list[str]:
    """`pieces` (sentences, a longer one already cut at its clauses) joined into requests of up to
    WHOLE_WORDS words; a short last one joins the one before it."""
    out: list[str] = []
    for piece in pieces:
        if out and _length(out[-1]) + _length(piece) <= WHOLE_WORDS:
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    if len(out) > 1 and _length(out[-1]) < WHOLE_MIN_WORDS:
        out[-2:] = [f"{out[-2]} {out[-1]}"]
    return out


def split_speech(text: str, *, first_max: int | None = None, whole: bool = False) -> list[str]:
    """Sentences, and long sentences split at commas/semicolons, each at most ~CHUNK_WORDS words
    where the text allows it; with `first_max`, the FIRST chunk is cut shorter still (at a comma
    if there is one, else at a word), so a slow TTS starts speaking sooner. With `whole` (a TTS
    that streams) sentences stay whole and go out together, up to WHOLE_WORDS words a request;
    only a sentence longer than that is cut, at its clauses. Joining the chunks gives back the
    same words."""
    limit = WHOLE_WORDS if whole else CHUNK_WORDS
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        if len(sentence.split()) <= limit:
            out.append(sentence)
            continue
        part: list[str] = []
        for clause in re.split(r"(?<=[,;:])\s+", sentence):
            if part and len(" ".join(part).split()) + len(clause.split()) > limit:
                out.append(" ".join(part))
                part = []
            part.append(clause)
        if part:
            out.append(" ".join(part))
    if first_max and not whole and out and len(out[0].split()) > first_max:
        words = out[0].split()
        cut = next(
            (i + 1 for i in range(min(first_max, len(words)) - 1, 2, -1) if words[i][-1] in ",;:"),
            first_max,
        )
        out[0:1] = [" ".join(words[:cut]), " ".join(words[cut:])]
    # A long stretch without punctuation is still cut, at words: no chunk over the limit.
    fitted: list[str] = []
    for chunk in out:
        words = chunk.split()
        fitted += [" ".join(words[i : i + limit]) for i in range(0, len(words), limit)]
    fitted = [c for c in fitted if c]
    return _whole_sentences(fitted) if whole else fitted


def _same_words(text: str) -> str:
    return " ".join(speakable(text).casefold().split())


def card_sentences(
    view: dict,
    *,
    skip: set[str] | frozenset = frozenset(),
    said: str = "",
    reasons: dict[str, str] | None = None,
) -> list[str]:
    """Fixed, server-rendered sentences for what an HQ turn left on screen (D8). The card text
    itself is not read out: the title and kind are enough to find it; the screen has the rest.

    ``said`` is what HQ's own answer already spoke on this turn: a question it contains WORD FOR
    WORD (case and spacing aside) is not asked a second time, only its options are read (#1911).
    Exact containment only: a paraphrase is asked again, so nothing goes unsaid.

    ``reasons``: why a card the call can't confirm by voice stays on screen (#2120), a FIXED
    sentence by card id, said before "You'll need to tap that one" so HQ is never taken to be
    unable to confirm anything by voice. A card HQ replaced (``replaced_by``) is not announced."""
    out: list[str] = []
    for p in view.get("proposals") or []:
        if p.get("action_id") in skip or p.get("replaced_by"):
            continue  # read back as a confirmable card instead, or replaced by a newer one
        kind = p.get("kind")
        title = (p.get("title") or "").strip()[:80] or "that session"
        if kind == "nudge":
            out.append(
                f"I've written a message for {title}. That one you'll want to check yourself."
            )
        elif kind == "session":
            out.append("I've set up a new session. That one you'll want to check yourself.")
        elif kind == "operation":
            label = (p.get("label") or p.get("verb") or "change").strip()[:60]
            why = (reasons or {}).get(p.get("action_id") or "", "")
            why = f" {why}" if why else ""
            out.append(
                f"I've got this ready: {label}, for {title}.{why} You'll need to tap that one."
            )
        elif kind == "message":
            # Reached only when the card is not confirmable by voice (#1833, Hermes 7610/7612): it
            # can't be read out word for word (too long, or not plain speech), so it's sent from the
            # screen.
            to = (p.get("recipient") or "").strip()[:80] or "your recipient"
            out.append(
                f"I've written a message to {to}. It's too long to read out word for word, "
                "so you'll need to tap that one to send it."
            )
    ask = view.get("ask")
    if isinstance(ask, dict) and ask.get("question"):
        opts = [o for o in ask.get("options") or [] if isinstance(o, str)]
        question = str(ask["question"]).strip()
        asked = bool(_same_words(question)) and _same_words(question) in _same_words(said)
        listed = or_list(opts[:4])  # "X, Y or Z?": HQ's own question, answered in any words
        line = " ".join(part for part in ("" if asked else question, listed) if part)
        if line:
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
    ("start", "it"), ("start",), ("do", "it"), ("do", "them"), ("go", "ahead"),
    ("approve", "it"), ("approve",), ("approved",),
)  # fmt: skip
_FILLERS = frozenset({"please", "ok", "okay", "yes", "yeah"})
#: The fillers that are themselves a yes: only these answer a batch's "Shall I go ahead with all?".
_YES = frozenset({"yes", "yeah", "ok", "okay"})
_SKIP = frozenset({"the", "number", "option", "please", "ok", "okay", "yes", "yeah"})
_CANCEL = re.compile(r"^(no|nope|cancel|cancel it|leave it|not now|never mind)\b")
#: Dismissing an HQ card by voice (#2120): one of these verbs, optionally its object, and then
#: what a confirm may carry (an ordinal, fillers). "No, dismiss it" is a dismiss; "no, don't
#: dismiss it" is not one (the extra word fails the allowlist) and stays a cancel.
_DISMISS_VERBS = frozenset({"dismiss", "drop", "discard", "delete", "scrap", "withdraw"})
_DISMISS_OBJECTS = (("it",), ("that",), ("this",), ("that", "card"), ("this", "card"),
                    ("the", "card"))  # fmt: skip
_DISMISS_LEAD = frozenset({"no", "nope", *_FILLERS})
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


def _dismiss_rest(words: list[str]) -> list[str] | None:
    """The words after a leading dismiss verb and its optional object, or None (#2120)."""
    i = 0
    while i < len(words) and words[i] in _DISMISS_LEAD:
        i += 1
    if i >= len(words) or words[i] not in _DISMISS_VERBS:
        return None
    rest = words[i + 1 :]
    for obj in sorted(_DISMISS_OBJECTS, key=len, reverse=True):
        if tuple(rest[: len(obj)]) == obj:
            return rest[len(obj) :]
    return rest


def _which(rest: list[str], cards: int, verdict: str) -> tuple[str, int | None]:
    """The card the words after a confirm or dismiss phrase name: ``(verdict, i)``, ``all``,
    ``ambiguous`` or ``other`` — one rule for both, so a dismiss is never looser than a confirm."""
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
    return verdict, picked or 0


def classify_confirmation(
    said: str, cards: int, *, dismiss: bool = False, batch: bool = False
) -> tuple[str, int | None]:
    """What a spoken reply to a read-back means, matched on the WHOLE utterance, never by a model:

    - ``("confirm", i)`` — fillers, one confirm phrase and, when more than one card is pending,
      exactly one ordinal naming card ``i``. Nothing else, and no question mark;
    - ``("dismiss", i)`` — only when ``dismiss`` (HQ cards, #2120): a dismiss verb, optionally
      "it"/"that", and the same ordinal rule as a confirm. The card is dismissed on its route;
    - ``("cancel", None)`` — leave it (the card stays on screen, untouched);
    - ``("all", None)`` — a bulk request: refused outside a batch, every card of one (#2389);
    - ``("dismiss_all", None)`` — "dismiss all": refused, never the batch's ``all``;
    - ``("ambiguous", None)`` — a confirm without the ordinal several cards need;
    - ``("other", None)`` — anything else: a new question, and the challenge is void.

    ``batch`` (#2389): the read-back asked "Shall I go ahead with all N?" (`batch_readback`), so a
    plain yes — fillers only ("yes", "yes please"), a confirm phrase with no ordinal ("go ahead"),
    or an "all" ("all of them", "do them all") — answers THAT question: ``("all", None)``. An
    ordinal still names one card; a dismiss still needs its number.
    """
    raw = said.translate(_APOSTROPHES).lower()
    norm = " ".join(re.sub(r"[^a-z0-9' ]", " ", raw).split())
    words = norm.split()
    if not words:
        return "other", None
    if batch and cards > 1 and "?" not in raw and len(words) <= CONFIRM_MAX_WORDS:
        rest = tuple(w for w in words if w not in _FILLERS)
        if not rest and any(w in _YES for w in words):
            return "all", None  # "yes", "yes please", "okay yes"
        if rest in _ALL_TAILS:
            return "all", None  # "all of them", "yes, all of them", "everything"
    if dismiss and "?" not in raw and len(words) <= CONFIRM_MAX_WORDS:
        rest = _dismiss_rest(words)
        if rest is not None:
            verdict, i = _which(rest, cards, "dismiss")
            if verdict == "ambiguous":
                return "ambiguous_dismiss", None  # "Which one should I drop?"
            if verdict == "all":
                # "Dismiss all" is refused, and is never the batch's `all` (#2389): that CONFIRMS.
                return "dismiss_all", None
            if verdict != "other":
                return verdict, i
    if _CANCEL.match(norm) or (not dismiss and words[0] == "dismiss"):
        return "cancel", None  # where there is no dismiss, "dismiss" still means leave it
    if "?" in raw or len(words) > CONFIRM_MAX_WORDS:
        return "other", None
    rest = _confirm_rest(words)
    if rest is None:
        return "other", None
    verdict, i = _which(rest, cards, "confirm")
    if batch and verdict == "ambiguous" and not [w for w in rest if w not in _SKIP]:
        return "all", None  # "go ahead", "do it", "confirm" to "Shall I go ahead with all N?"
    return verdict, i


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
_MENU_ORDINAL_NAMES = ("first", "second", "third", "fourth", "fifth")


#: The question a read-back ends on, by the card's verb (#2342): asked the way a person asks, never
#: an instruction about which words to say. HQ cards are judged (#1715), so any natural yes counts.
_ASK_BY_VERB = {
    "send it": "Want me to send it?",
    "start it": "Shall I start it?",
    "save it": "Shall I save it?",
    "do it": "Shall I go ahead?",
    "use it": "Shall I use it?",
}


def which_of(n: int) -> str:
    """ "the first or the second", "the first, the second or the third" (#2342)."""
    names = [f"the {w}" for w in _ORDINAL_WORDS[: max(0, min(n, len(_ORDINAL_WORDS)))]]
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + f" or {names[-1]}"


def or_list(labels: list[str]) -> str:
    """ "X or Y?", "X, Y or Z?": choices offered the way a person offers them (#2342)."""
    items = [str(x).strip().rstrip(".?!") for x in labels if str(x).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return f"{items[0]}?"
    return ", ".join(items[:-1]) + f" or {items[-1]}?"


def ordinal_list(labels: list[str]) -> str:
    """ "The first is X, the second is Y and the third is Z.": a list whose reply is still picked
    by its place (the Needs-you grammar, `classify_decision`), read the way a person reads one
    out, never "one, X; two, Y" (#2342)."""
    items = [str(x).strip().rstrip(".") for x in labels if str(x).strip()]
    parts = [f"the {_MENU_ORDINAL_NAMES[i]} is {x}" for i, x in enumerate(items[:5])]
    if not parts:
        return ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return text[0].upper() + text[1:] + "."


#: Verbs that are themselves a confirm phrase of `classify_confirmation`'s grammar.
_GRAMMAR_VERBS = frozenset({"send it", "start it", "do it"})


def grammar_ask(verb: str) -> str:
    """The question for a card the judge does NOT hear (a connector message, #1833; any batch
    holding one): it names a phrase the English grammar accepts, since a bare "yes" there is not
    one (Hermes 8839): "Send it, or cancel?" / "Confirm it, or cancel?"."""
    phrase = verb if verb in _GRAMMAR_VERBS else "confirm it"
    return f"{phrase[0].upper()}{phrase[1:]}, or cancel?"


_NUMBERS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
)  # fmt: skip


def number_word(n: int) -> str:
    """ "twelve": a count said the way a person says it (digits past twenty)."""
    return _NUMBERS[n] if 0 <= n < len(_NUMBERS) else str(n)


#: What a batch read-back calls N cards of one kind (#2389): ``(one, several)``, by the card's
#: operation id, else its kind. Every kind is named, so a riskier card never hides behind
#: "renames"; an operation not listed is named by its own catalog label ("tag changes").
_BATCH_NOUNS = {
    "nudge": ("message", "messages"),
    "session": ("new session", "new sessions"),
    "message": ("message", "messages"),
    "session.rename": ("rename", "renames"),
    "session.archive": ("session to archive", "sessions to archive"),
    "session.unarchive": ("session to unarchive", "sessions to unarchive"),
    "session.close": ("session to close", "sessions to close"),
    "session.move": ("session to move", "sessions to move"),
    "session.tag": ("tag change", "tag changes"),
    "session.color": ("colour change", "colour changes"),
    "project.rename": ("project rename", "project renames"),
    "project.archive": ("project to archive", "projects to archive"),
    "project.unarchive": ("project to unarchive", "projects to unarchive"),
    "project.delete": ("project to delete", "projects to delete"),
    "project.create": ("new project", "new projects"),
    "project.add_folder": ("folder to add", "folders to add"),
    "playbook.create": ("new playbook", "new playbooks"),
    "skill.create": ("new skill", "new skills"),
    "automation.create": ("new automation", "new automations"),
}


def batch_group(card: dict) -> str:
    """The kind a batch read-back and summary count a card under: its operation, else its kind."""
    return str(card.get("op") or card.get("kind") or "")


def _batch_noun(card: dict, n: int) -> str:
    one, several = _BATCH_NOUNS.get(batch_group(card), ("", ""))
    if not one:
        label = " ".join(str(card.get("label") or "").split())[:40].lower()
        one, several = (f"{label} change", f"{label} changes") if label else ("change", "changes")
    return one if n == 1 else several


def batch_kinds(cards: list[dict]) -> str:
    """ "Twelve renames", "Three renames and two sessions to archive": every kind of a batch, in
    the order the cards came, each with its count (#2389)."""
    groups: dict[str, list[dict]] = {}
    for c in cards:
        groups.setdefault(batch_group(c), []).append(c)
    parts = [f"{number_word(len(g))} {_batch_noun(g[0], len(g))}" for g in groups.values()]
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return text[0].upper() + text[1:]


def batch_question(n: int) -> str:
    """The question a batch read-back ends on (#2389): "Shall I go ahead with all twelve?". It
    shares no three-word run with the replies it invites ("do them all", "yes, all of them"), so
    the echo cut (`strip_echo`) never takes a quick answer for HQ's own words."""
    if n == 2:
        return "Shall I go ahead with both?"
    return f"Shall I go ahead with all {number_word(n)}?"


def batch_readback(cards: list[dict]) -> str:
    """The read-back of two or more JUDGED cards (#2389): their count and what they do, in ONE
    sentence, never each card word for word (the operator, #2070 141364). The cards' own texts are
    on the screen, and the judge is shown each card's descriptors as data (`voice_judge`)."""
    return f"{batch_kinds(cards)}, the details are on your screen. {batch_question(len(cards))}"


def readback(cards: list[dict], *, judged: bool = True) -> str:
    """The fixed read-back that arms a challenge: what each card does, then ONE plain question
    (#2342): "Want me to send it?", never "Say confirm to …". ``judged``: whether the reply goes to
    the judge (#1715, every card of `voice_calls.JUDGED_KINDS`); otherwise the question names a
    phrase the grammar accepts, so the answer it invites is one the call can take. Two or more
    judged cards are read back SHORT (`batch_readback`, #2389)."""
    if not cards:
        return ""
    if judged and len(cards) > 1 and not any(c.get("ask") for c in cards):
        return batch_readback(cards)
    if len(cards) == 1:
        c = cards[0]
        if c.get("ask"):  # a Needs-you item decided by its own words (#1681 P2)
            return f"{c['line']} {c['ask']}"
        verb = c.get("verb") or ""
        if not judged:
            return f"{c['line']} {grammar_ask(verb)}"
        return f"{c['line']} {_ASK_BY_VERB.get(verb, 'Shall I go ahead?')}"
    parts = [f"{_ORDINAL_WORDS[i].capitalize()}: {c['line']}" for i, c in enumerate(cards)]
    if not judged:
        return " ".join(parts) + f" Confirm {which_of(len(cards))}, or cancel?"
    return " ".join(parts) + f" Which one should I do, {which_of(len(cards))}?"


def readback_screen_only(cards: list[dict]) -> str:
    """The read-back of cards that are NOT judged (#1715: Needs-you decisions, picks) on a call
    whose language the English matcher does not cover: what each card does, confirmed on the
    screen only. HQ cards are judged in any language and never read this way. (English: HQ
    answers in the call's language, but this fixed line stays short and plain.)"""
    if not cards:
        return ""
    parts = [c["line"] for c in cards]
    return " ".join(parts) + " You'll need to tap that one."


def batch_summary(
    *,
    total: int,
    done: int = 0,
    running: int = 0,
    failed: int = 0,
    unknown: int = 0,
    skipped: int = 0,
    stopped: int = 0,
    on_screen: int = 0,
) -> str:
    """The ONE line a batch ends on (#2389), from its counted results: what was done, what is
    still running, what failed, what can't be told, what the Commander tapped themselves and what
    was not attempted once they spoke. ``on_screen``: of the failed and unknown cards, those the
    store says are STILL pending, re-read just before this line; only they are said to be on the
    screen. A FIXED sentence: counts only, never a card's text."""
    if total > 1 and done == total:
        return f"Done, all {number_word(total)}."
    if total == 1 and done == 1:
        return "Done."
    clauses = [
        f"{number_word(n)} {what}"
        for n, what in (
            (done, "done"),
            (running, "still running"),
            (failed, "didn't go through"),
            (unknown, "I can't tell yet"),
            (skipped, "you'd tapped already"),
            (stopped, "not started"),
        )
        if n
    ]
    if not clauses:
        return "Nothing was done."
    text = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + f" and {clauses[-1]}"
    out = [text[0].upper() + text[1:] + "."]
    if stopped:
        out.append("I stopped there.")
    if on_screen == 1:
        out.append("That one is still on your screen.")
    elif on_screen > 1:
        out.append(f"Those {number_word(on_screen)} are still on your screen.")
    return " ".join(out)
