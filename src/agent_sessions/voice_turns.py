"""Which transcripts belong to which spoken turn (#1397). No Pipecat imports, so CI tests it.

The worker's STT runs one segment at a time, in order (Pipecat's `SegmentedSTTService`), so the
n-th result back (a transcript, or an empty/failed segment) is the n-th segment sent. A turn ends
at a stop; it owns the segments sent since the previous turn's end, up to its own. Its text is
exactly those segments' transcripts — never a neighbour's — delivered once they are all back (the
worker waits for them, bounded only by the STT timeout). A transcript whose turn already went out
is LATE: delivered once, on its own, with its turn (`late_turn`).

A turn is heard in a language only when every segment with words in it was (#1397 P3): an empty
segment (a breath) has none, and mixed or unreported ones leave the turn with none.
"""

from __future__ import annotations

#: How many recent turns a LATE transcript can still be traced to.
OWNERS_KEPT = 32


def stop_instant(vad_stopped_at: float | None, now: float) -> float:
    """When the Commander stopped, for `user_stopped`'s `at` (#1845, #1996): the instant the
    voice detector ended the turn's LAST segment, not the instant the turn was declared over.
    The two are far apart when the turn model takes a sentence for unfinished and waits for more
    (`voice_calls.WORKER_TURN_WAIT_S`), and the app times its commit pause from this instant: it
    promises an answer after that much silence, not after that much on top of the wait. No
    segment seen, or a clock that makes no sense: now."""
    if vad_stopped_at is None or vad_stopped_at > now:
        return now
    return vad_stopped_at


class Turns:
    def __init__(self) -> None:
        self.sent = 0  # segments handed to STT
        self.done = 0  # results back, in order (transcripts + empty/failed)
        self.flushed_upto = 0  # segments up to here belong to turns already delivered
        self._heard: dict[int, str] = {}  # segment index → its transcript, not yet delivered
        self._langs: dict[int, str | None] = {}  # segment index → the language STT reported
        self.flushed_language: str | None = None  # the language of the turn last flushed
        self.late_language: str | None = None  # the language of the last LATE transcript
        self.late_turn: int | None = None  # the turn the last LATE transcript belongs to (#1842)
        self._owners: list[tuple[int, int | None]] = []  # (last segment, turn) per flush, recent

    def segment_sent(self) -> None:
        self.sent += 1

    def segment_empty(self) -> None:
        self.done += 1

    def transcript(self, text: str, language: str | None = None) -> str | None:
        """Record the next result (and the language STT reported for it); returns it if it is
        LATE (its turn already went out), with its language in `late_language`."""
        self.done += 1
        if self.done <= self.flushed_upto:
            self.late_language = language
            # Its own turn (Hermes 7641): the first flush whose segments reach this one.
            self.late_turn = next((t for upto, t in self._owners if self.done <= upto), None)
            return text
        self._heard[self.done] = text
        self._langs[self.done] = language
        return None

    def target(self) -> int:
        """The last segment of the turn ending now."""
        return self.sent

    def settled(self, target: int) -> bool:
        return self.done >= target

    def flush(self, target: int, turn: int | None = None) -> str:
        """This turn's text: the transcripts of ITS segments only (up to `target`). Newer ones
        stay for the next turn; its segments still out will arrive late, tagged with `turn`."""
        mine = sorted(i for i in self._heard if i <= target)
        langs = {self._langs.pop(i, None) for i in mine if self._heard[i]}
        text = " ".join(self._heard.pop(i) for i in mine if self._heard[i]).strip()
        for i in mine:
            self._heard.pop(i, None)
            self._langs.pop(i, None)
        self.flushed_language = langs.pop() if len(langs) == 1 else None
        self.flushed_upto = max(self.flushed_upto, target)
        self._owners.append((target, turn))
        del self._owners[:-OWNERS_KEPT]
        return text


def ended_unfinished(vad_stopped_at: float | None, now: float, turn_wait: float | None) -> bool:
    """Whether the turn model took the sentence for UNFINISHED and the turn ended only because
    the Commander stayed quiet for the whole `turn_wait` (#1996). A sentence it takes for
    finished ends within a fraction of a second of the voice detector's stop; one it does not
    ends `turn_wait` after it. The app waits longer before it answers such a turn: someone who
    trails off mid-thought is more likely to go on."""
    if vad_stopped_at is None or not turn_wait or turn_wait <= 0:
        return False
    return now - vad_stopped_at >= turn_wait * 0.9


def stt_language_fields(
    features: frozenset | set, listen: frozenset | None, detected: str | None
) -> dict:
    """The optional transcription fields one request carries (#1996), each ONLY if the server
    listed it: `languages` (the ones the call listens in) and `language_hint` (the call's
    language so far). They are two features: a server that lists `languages` alone is never
    sent a hint, which a strict server would refuse and the call would end on. A hint is only
    ever sent beside the list it belongs to."""
    if "languages" not in features or not listen:
        return {}
    out: dict = {"languages": tuple(sorted(listen))}
    if "language_hint" in features and detected in listen:
        out["language_hint"] = detected
    return out
