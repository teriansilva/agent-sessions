"""#1626 review: a barge-in stops EVERY synthesis — the sentence playing and the look-ahead —
because Pipecat's interruption clears playback without closing the running TTS generator."""

import asyncio

from agent_sessions.voice_synth import Synths


class _S:
    def __init__(self, text):
        self.text = text
        self.task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))


def test_drop_cancels_the_playing_sentence_and_the_lookahead():
    async def go():
        s = Synths(_S, lookahead=1)
        s.prefetch(["one", "two", "three"])
        playing = s.start("one")
        ahead = list(s.ahead.values())
        assert s.active is playing and ahead
        s.drop()
        await asyncio.sleep(0)
        assert playing.task.cancelled() and all(a.task.cancelled() for a in ahead)
        assert s.active is None and not s.ahead and not s.pending

    asyncio.run(go())


def test_a_finished_sentence_is_no_longer_active():
    async def go():
        s = Synths(_S, lookahead=0)
        playing = s.start("one")
        s.finished(playing)
        assert s.active is None
        playing.task.cancel()

    asyncio.run(go())
