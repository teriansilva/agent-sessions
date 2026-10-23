"""The sentences the worker is synthesizing: the one playing and a look-ahead (#1397).

No Pipecat imports, so CI tests it. A barge-in (`drop`) cancels ALL of them — the look-ahead and
the sentence playing right now — because an interruption clears playback without closing the TTS
generator that is still pulling audio from the endpoint.
"""

from __future__ import annotations

from collections.abc import Callable


class Synths:
    def __init__(self, make: Callable[[str], object], lookahead: int) -> None:
        self._make = make  # text → a synth with a `.task` (an asyncio task)
        self._lookahead = lookahead
        self.ahead: dict[str, object] = {}
        self.pending: list[str] = []
        self.active: object | None = None

    def prefetch(self, texts: list[str]) -> None:
        self.pending.extend(texts)
        self._top_up(started=None)

    def _top_up(self, started: str | None) -> None:
        if started in self.pending:
            self.pending.remove(started)
        for t in self.pending[: self._lookahead + (1 if started is None else 0)]:
            if t not in self.ahead:
                self.ahead[t] = self._make(t)

    def start(self, text: str):
        """The synth for the sentence starting to play now (prefetched, or made now)."""
        synth = self.ahead.pop(text, None) or self._make(text)
        self.active = synth
        self._top_up(started=text)
        return synth

    def finished(self, synth) -> None:
        if self.active is synth:
            self.active = None

    def drop(self) -> None:
        """Barge-in: cancel the sentence playing AND everything synthesized ahead."""
        for synth in [*self.ahead.values(), *([self.active] if self.active else [])]:
            synth.task.cancel()
        self.ahead.clear()
        self.pending.clear()
        self.active = None
