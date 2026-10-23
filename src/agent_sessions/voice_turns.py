"""Which transcripts belong to which spoken turn (#1397). No Pipecat imports, so CI tests it.

The worker's STT runs one segment at a time, in order (Pipecat's `SegmentedSTTService`), so the
n-th result back (a transcript, or an empty/failed segment) is the n-th segment sent. A turn ends
at a stop; it owns the segments sent since the previous turn's end, up to its own. Its text is
exactly those segments' transcripts — never a neighbour's — delivered once they are all back or
after a grace. A transcript whose turn already went out is LATE: delivered once, on its own.
"""

from __future__ import annotations


class Turns:
    def __init__(self) -> None:
        self.sent = 0  # segments handed to STT
        self.done = 0  # results back, in order (transcripts + empty/failed)
        self.flushed_upto = 0  # segments up to here belong to turns already delivered
        self._heard: dict[int, str] = {}  # segment index → its transcript, not yet delivered

    def segment_sent(self) -> None:
        self.sent += 1

    def segment_empty(self) -> None:
        self.done += 1

    def transcript(self, text: str) -> str | None:
        """Record the next result; returns it if it is LATE (its turn already went out)."""
        self.done += 1
        if self.done <= self.flushed_upto:
            return text
        self._heard[self.done] = text
        return None

    def target(self) -> int:
        """The last segment of the turn ending now."""
        return self.sent

    def settled(self, target: int) -> bool:
        return self.done >= target

    def flush(self, target: int) -> str:
        """This turn's text: the transcripts of ITS segments only (up to `target`). Newer ones
        stay for the next turn; its segments still out will arrive late."""
        mine = sorted(i for i in self._heard if i <= target)
        text = " ".join(self._heard.pop(i) for i in mine if self._heard[i]).strip()
        self.flushed_upto = max(self.flushed_upto, target)
        return text
