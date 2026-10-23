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


def test_each_sentence_is_synthesized_with_its_own_says_parameters():
    """#1635 review: a later say switching voice/language must not reach the sentences still
    queued (or made lazily as look-ahead) from an earlier say."""

    async def go():
        made: list[tuple] = []

        def make(key):
            made.append(key)
            return _S(key)

        s = Synths(make, lookahead=1)
        s.say(["one", "two", "three"], "de-voice")
        s.begin("one", "now")  # "two" is synthesized ahead; "three" not yet
        s.say(["four"], "en-voice")  # a later say switches the voice
        for t in ("two", "three", "four"):
            s.begin(t, "now")
        assert dict(made) == {"one": "de-voice", "two": "de-voice", "three": "de-voice",
                              "four": "en-voice"}  # fmt: skip
        s.begin("never queued", "now")
        assert made[-1] == ("never queued", "now")
        s.drop()

    asyncio.run(go())


def test_drop_forgets_the_queued_sentences_parameters():
    async def go():
        made: list[tuple] = []
        s = Synths(lambda k: made.append(k) or _S(k), lookahead=0)
        s.say(["one"], "old")
        s.drop()
        s.begin("one", "current")  # re-said after a barge-in: today's parameters
        assert made[-1] == ("one", "current")
        s.drop()

    asyncio.run(go())
