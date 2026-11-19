"""The peer channel (#1246 §3): the handshake's trust inputs, calls over streams, the closed verb
table and the budgets."""

from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_sessions.federation import channel as ch
from agent_sessions.federation import noise
from agent_sessions.homefree import mux as hf_mux
from federation_helpers import pipe, until


def run(coro):
    return asyncio.run(coro)


def _no_args(args):
    if args:
        raise ch.BadArgs("none expected")
    return {}


async def _echo(channel, args):
    return {"got": args}


async def _slow(channel, args):
    await asyncio.sleep(30)
    return {}


async def _boom(channel, args):
    raise ch.VerbError("nope")


def _echo_args(args):
    if set(args) - {"text"} or not isinstance(args.get("text", ""), str):
        raise ch.BadArgs("text")
    return args


VERBS = {
    v.name: v
    for v in (
        ch.Verb("t.echo", frozenset({ch.MODE_PEER}), _echo_args, _echo),
        ch.Verb("t.slow", frozenset({ch.MODE_PEER}), _no_args, _slow),
        ch.Verb("t.boom", frozenset({ch.MODE_PEER}), _no_args, _boom),
        ch.Verb("t.pair_only", frozenset({ch.MODE_PAIR}), _no_args, _echo),
    )
}


async def _connect(*, budgets=None, verbs=VERBS):
    a_key, b_key = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    a_end, b_end = pipe()
    est_a, est_b = await asyncio.gather(
        ch.initiate(a_end, a_key, mode=ch.MODE_PEER, expect_static=noise.public_bytes(b_key)),
        ch.respond(b_end, b_key, psk_for=lambda mode, invite: None),
    )
    a = ch.Channel(a_end, est_a, verbs=verbs, budgets=budgets)
    b = ch.Channel(b_end, est_b, verbs=verbs, budgets=budgets)
    tasks = [asyncio.create_task(a.run()), asyncio.create_task(b.run())]
    return a, b, a_end, b_end, tasks


async def _shutdown(a, b, tasks):
    a.close()
    b.close()
    await asyncio.gather(*tasks)


def test_a_call_round_trips_in_both_directions():
    async def go():
        a, b, _, _, tasks = await _connect()
        assert await a.call("t.echo", {"text": "hi"}) == {"got": {"text": "hi"}}
        assert await b.call("t.echo", {"text": "yo"}) == {"got": {"text": "yo"}}
        assert a.remote_static != b.remote_static
        assert a.handshake_hash == b.handshake_hash
        await _shutdown(a, b, tasks)

    run(go())


def test_nothing_on_the_wire_is_plaintext():
    async def go():
        a, b, a_end, b_end, tasks = await _connect()
        await a.call("t.echo", {"text": "a-very-recognisable-needle"})
        wire = b"".join(a_end.sent[1:]) + b"".join(b_end.sent)  # all but the preamble
        assert b"needle" not in wire and b"t.echo" not in wire
        await _shutdown(a, b, tasks)

    run(go())


def test_the_initiator_refuses_an_unexpected_responder_before_sending_its_key():
    async def go():
        a_key, b_key = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        a_end, b_end = pipe()
        responder = asyncio.create_task(ch.respond(b_end, b_key, psk_for=lambda mode, invite: None))
        with pytest.raises(ch.HandshakeRefused):
            await ch.initiate(
                a_end, a_key, mode=ch.MODE_PEER, expect_static=noise.public_bytes(a_key)
            )
        # preamble + message one only: the initiator's static key (message three) never left
        assert len(a_end.sent) == 2
        await a_end.close()
        with pytest.raises(ch.HandshakeRefused):
            await responder

    run(go())


def test_a_changed_preamble_fails_the_handshake():
    async def go():
        a_key, b_key = X25519PrivateKey.generate(), X25519PrivateKey.generate()
        a_end, b_end = pipe()

        class Tamper:
            """Rewrites the preamble in transit, as a relay or proxy could."""

            def __init__(self, inner):
                self.inner, self.first = inner, True

            async def recv(self):
                data = await self.inner.recv()
                if self.first:
                    self.first = False
                    return ch.preamble(ch.MODE_PEER) + b" "
                return data

            async def send(self, data):
                await self.inner.send(data)

            async def close(self):
                await self.inner.close()

        responder = asyncio.create_task(
            ch.respond(Tamper(b_end), b_key, psk_for=lambda mode, invite: None)
        )
        with pytest.raises(ch.HandshakeRefused):
            await ch.initiate(
                a_end, a_key, mode=ch.MODE_PEER, expect_static=noise.public_bytes(b_key)
            )
        await a_end.close()
        with pytest.raises(ch.HandshakeRefused):
            await responder

    run(go())


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b'{"v":2,"mode":"peer"}',
        b'{"v":1,"mode":"admin"}',
        b'{"v":1,"mode":"peer","invite":"abcdefgh"}',
        b'{"v":1,"mode":"pair"}',
        b'{"v":1,"mode":"pair","invite":"../../etc"}',
        b'{"v":1,"mode":"peer","x":1}',
        b"[" * 300,
    ],
)
def test_a_malformed_preamble_is_refused_before_any_crypto(raw):
    async def go():
        a_end, b_end = pipe()
        await a_end.send(raw)
        calls = []

        def psk_for(mode, invite):
            calls.append(mode)
            return None

        with pytest.raises(ch.HandshakeRefused):
            await ch.respond(b_end, X25519PrivateKey.generate(), psk_for=psk_for)
        assert calls == []
        assert b_end.sent == []

    run(go())


def test_the_handshake_has_a_deadline(monkeypatch):
    monkeypatch.setattr(ch, "HANDSHAKE_TIMEOUT_S", 0.05)

    async def go():
        _, b_end = pipe()
        with pytest.raises(ch.HandshakeRefused, match="timed out"):
            await ch.respond(b_end, X25519PrivateKey.generate(), psk_for=lambda m, i: None)

    run(go())


@pytest.mark.parametrize(
    "info",
    [
        {"verb": "t.unknown", "args": {}},
        {"verb": "t.pair_only", "args": {}},  # a real verb, outside this channel's mode
        {"verb": "t.echo", "args": {"text": 5}},
        {"verb": "t.echo", "args": {"path": "/etc/passwd"}},
        {"verb": "t.echo", "args": []},
        {"verb": 7, "args": {}},
        {"verb": "t.echo"},
        {"verb": "t.echo", "args": {}, "extra": 1},
        ["t.echo"],
    ],
)
def test_anything_outside_the_verb_table_resets_the_stream(info):
    async def go():
        a, b, _, _, tasks = await _connect()
        stream = a._mux.open(json.dumps(info).encode())
        await stream.end()
        with pytest.raises(hf_mux.StreamReset) as exc:
            await stream.read()
        assert exc.value.code == ch.RESET_REFUSED
        assert b.stats.refused == 1 and b.stats.calls_in == 0
        # the channel itself is still fine
        assert await a.call("t.echo", {"text": "ok"}) == {"got": {"text": "ok"}}
        await _shutdown(a, b, tasks)

    run(go())


def test_call_reports_a_refused_verb():
    async def go():
        a, b, _, _, tasks = await _connect()
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.unknown")
        assert exc.value.code == "refused"
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.boom")
        assert exc.value.code == "nope"
        await _shutdown(a, b, tasks)

    run(go())


def test_repeated_violations_close_the_channel():
    async def go():
        a, b, _, _, tasks = await _connect(budgets=ch.Budgets(violations=3))
        for _ in range(3):
            with pytest.raises(ch.CallError):
                await a.call("t.unknown")
        await until(lambda: b.closed)
        assert b.close_reason == "too many violations"
        await until(lambda: a.closed)
        await asyncio.gather(*tasks)

    run(go())


def test_the_stream_budget_refuses_instead_of_queueing():
    async def go():
        a, b, _, _, tasks = await _connect(budgets=ch.Budgets(streams=2))
        slow = [asyncio.create_task(a.call("t.slow", timeout=5)) for _ in range(2)]
        await until(lambda: len(b._handlers) == 2)
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.echo", {"text": "x"})
        assert exc.value.code == "busy"
        assert b.stats.busy == 1
        for task in slow:
            task.cancel()
        await asyncio.gather(*slow, return_exceptions=True)
        await _shutdown(a, b, tasks)

    run(go())


def test_the_rate_budget_refuses():
    async def go():
        a, b, _, _, tasks = await _connect(budgets=ch.Budgets(calls_per_minute=3))
        for _ in range(3):
            await a.call("t.echo", {"text": "x"})
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.echo", {"text": "x"})
        assert exc.value.code == "busy"
        await _shutdown(a, b, tasks)

    run(go())


def test_a_sender_that_ignores_the_window_is_reset():
    async def go():
        a, b, _, _, tasks = await _connect()
        received = []
        # Open a stream by hand and push more than one window at the answering side without
        # waiting for credit, as a hostile peer would.
        stream = a._mux.open(json.dumps({"verb": "t.slow", "args": {}}).encode())
        await until(lambda: len(b._handlers) == 1)
        chunk = b"x" * hf_mux.MAX_CHUNK
        for _ in range(hf_mux.INITIAL_WINDOW // hf_mux.MAX_CHUNK + 2):
            a._mux._emit(hf_mux._encode(stream.id, hf_mux.DATA, chunk))
        with pytest.raises(hf_mux.StreamReset) as exc:
            await stream.read()
        received.append(exc.value.code)
        assert received == [ch.RESET_OVERRUN]
        assert not b.closed
        await _shutdown(a, b, tasks)

    run(go())


def test_an_oversize_open_info_never_reaches_a_handler():
    async def go():
        a, b, _, _, tasks = await _connect()
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.echo", {"text": "x" * (ch.OPEN_INFO_MAX + 1)})
        assert exc.value.code == "too_large"
        assert b.stats.calls_in == 0
        await _shutdown(a, b, tasks)

    run(go())


def test_a_tampered_frame_ends_the_channel():
    async def go():
        a, b, a_end, b_end, tasks = await _connect()
        await a.call("t.echo", {"text": "x"})
        b_end._inbox.put_nowait(b"\x00" * 40)  # not a valid Noise message for the next nonce
        await until(lambda: b.closed)
        assert b.close_reason == "bad frame"
        await until(lambda: a.closed)
        await asyncio.gather(*tasks)

    run(go())


def test_a_call_on_a_closed_channel_fails_fast():
    async def go():
        a, b, _, _, tasks = await _connect()
        await _shutdown(a, b, tasks)
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.echo", {"text": "x"})
        assert exc.value.code == "closed"
        # refused before anything was queued: the request provably never left (#2061)
        assert exc.value.dispatched is False

    run(go())


def test_a_channel_that_ends_under_a_call_does_not_say_the_request_never_left():
    """The dispatch boundary (#2061): once `call` handed the mux its frame, a channel that ends
    is `closed` with `dispatched` true. Whether the other side got it is not known here."""

    async def go():
        a, b, _, _, tasks = await _connect()
        call = asyncio.create_task(a.call("t.slow", timeout=5))
        await until(lambda: b.stats.calls_in == 1)  # the other side admitted it
        a.close("test")
        with pytest.raises(ch.CallError) as exc:
            await call
        assert exc.value.code == "closed" and exc.value.dispatched is True
        assert ch.CallError("anything").dispatched is True  # the default is the unsafe-to-assume
        await _shutdown(a, b, tasks)

    run(go())


def test_a_call_times_out_and_frees_the_stream():
    async def go():
        a, b, _, _, tasks = await _connect()
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.slow", timeout=0.05)
        assert exc.value.code == "timeout"
        assert a._mux._streams == {}
        await _shutdown(a, b, tasks)

    run(go())


def test_an_answered_stream_is_forgotten_even_if_the_caller_never_ends_it():
    async def go():
        a, b, _, _, tasks = await _connect()
        stream = a._mux.open(json.dumps({"verb": "t.echo", "args": {}}).encode())
        body = await stream.read()  # never half-closes its side
        assert json.loads(body)["ok"] is True
        await until(lambda: b._mux._streams == {})
        await _shutdown(a, b, tasks)

    run(go())


# ── the admission boundary in front of the mux ────────────────────────


def _open(stream_id: int, verb: str = "t.echo") -> bytes:
    return hf_mux._encode(stream_id, hf_mux.OPEN, json.dumps({"verb": verb, "args": {}}).encode())


def test_a_reused_wrong_parity_or_backwards_stream_id_is_refused_before_allocation():
    async def go():
        a, b, _, _, tasks = await _connect()
        # `a` is the initiator and opens odd ids. Open 5, then try: 5 again, an even id, and 3.
        a._mux._emit(_open(5, "t.slow"))
        await until(lambda: b._mux.peer_streams == 1)
        for bad in (5, 4, 3):
            a._mux._emit(_open(bad))
        await until(lambda: b.stats.refused == 3)
        assert b._mux.peer_streams == 1 and set(b._mux._streams) == {5}
        assert b.stats.calls_in == 1
        await _shutdown(a, b, tasks)

    run(go())


def test_the_stream_cap_refuses_an_open_without_allocating_a_stream():
    async def go():
        a, b, _, _, tasks = await _connect(budgets=ch.Budgets(streams=2))
        for sid in (1, 3):
            a._mux._emit(_open(sid, "t.slow"))
        await until(lambda: len(b._handlers) == 2)
        a._mux._emit(_open(7))
        await until(lambda: b.stats.busy == 1)
        assert set(b._mux._streams) == {1, 3}  # nothing was created for the refused one
        await _shutdown(a, b, tasks)

    run(go())


def test_data_after_end_is_a_violation():
    async def go():
        a, b, _, _, tasks = await _connect()
        a._mux._emit(_open(1, "t.slow"))
        a._mux._emit(hf_mux._encode(1, hf_mux.END))
        await until(lambda: len(b._handlers) == 1)
        a._mux._emit(hf_mux._encode(1, hf_mux.DATA, b"late"))
        await until(lambda: b.stats.refused == 1)
        assert b._mux._streams == {}
        await _shutdown(a, b, tasks)

    run(go())


def test_the_frame_cap_is_exact_and_legal_for_noise():
    assert ch.FRAME_MAX + noise.TAGLEN <= noise.MAX_MESSAGE  # one frame fits one Noise message
    assert hf_mux.MAX_CHUNK + 8 <= ch.FRAME_MAX and ch.OPEN_INFO_MAX + 8 <= ch.FRAME_MAX

    async def go():
        a, b, a_end, b_end, tasks = await _connect()
        # exactly the cap is carried (an unknown stream id: the mux ignores it, the channel lives)
        a._enqueue(hf_mux._encode(99, hf_mux.DATA, b"x" * (ch.FRAME_MAX - 8)))
        assert await a.call("t.echo", {"text": "ok"}) == {"got": {"text": "ok"}}
        assert max(len(w) for w in a_end.sent) == ch.WIRE_MAX
        # one byte more is never sent: the sender closes instead
        a._enqueue(hf_mux._encode(99, hf_mux.DATA, b"x" * (ch.FRAME_MAX - 7)))
        await until(lambda: a.closed)
        assert a.close_reason == "send overflow"
        await asyncio.gather(*tasks)

    run(go())


def test_an_oversize_wire_frame_closes_the_receiver_before_decrypting():
    async def go():
        a, b, a_end, b_end, tasks = await _connect()
        b_end._inbox.put_nowait(b"\x00" * (ch.WIRE_MAX + 1))
        await until(lambda: b.closed)
        assert b.close_reason == "oversize frame"
        await asyncio.gather(*tasks)

    run(go())


def test_a_large_answer_is_chunked_and_an_oversize_one_is_refused():
    big = "y" * (ch.ANSWER_MAX - 64)

    async def _big(channel, args):
        return {"blob": big}

    async def _huge(channel, args):
        return {"blob": "y" * (ch.ANSWER_MAX + 1)}

    verbs = {
        "t.big": ch.Verb("t.big", frozenset({ch.MODE_PEER}), _no_args, _big),
        "t.huge": ch.Verb("t.huge", frozenset({ch.MODE_PEER}), _no_args, _huge),
    }

    async def go():
        a, b, a_end, b_end, tasks = await _connect(verbs=verbs)
        assert (await a.call("t.big"))["blob"] == big
        assert max(len(w) for w in b_end.sent) <= ch.WIRE_MAX
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.huge")
        assert exc.value.code == ch.ANSWER_LOST  # the handler ran: never "too_large" (#2061)
        await _shutdown(a, b, tasks)

    run(go())


def test_the_byte_budget_delays_reading_instead_of_buffering():
    async def go():
        a, b, _, _, tasks = await _connect(budgets=ch.Budgets(bytes_per_minute=600_000))
        b._bytes_in._tokens = 0.0  # the minute's allowance is spent
        loop = asyncio.get_running_loop()
        started = loop.time()
        # ~4 KiB at 10 KiB/s: b reads this frame, then waits ~0.4 s before it acts on it
        assert await a.call("t.echo", {"text": "z" * 4000}) == {"got": {"text": "z" * 4000}}
        assert 0.3 < loop.time() - started < 5
        await _shutdown(a, b, tasks)

    run(go())


def test_the_bucket_reports_how_long_an_overdraft_takes_to_clear():
    bucket = ch._Bucket(600)  # 10 a second
    assert bucket.debt(600) == 0.0  # exactly the allowance: nothing to wait for
    assert 0.9 < bucket.debt(10) <= 1.0  # ten over: about one second
    assert bucket.take() is False


def test_an_answer_past_the_byte_budget_is_withheld_as_lost_never_as_busy():
    async def _blob(channel, args):
        return {"blob": "y" * 5000}

    verbs = {"t.blob": ch.Verb("t.blob", frozenset({ch.MODE_PEER}), _no_args, _blob)}

    async def go():
        a, b, _, _, tasks = await _connect(verbs=verbs, budgets=ch.Budgets(bytes_per_minute=8000))
        assert len((await a.call("t.blob"))["blob"]) == 5000
        with pytest.raises(ch.CallError) as exc:
            await a.call("t.blob")
        assert exc.value.code == ch.ANSWER_LOST and b.stats.busy == 1
        await _shutdown(a, b, tasks)

    run(go())


def test_an_answer_suppressed_after_the_handler_ran_never_says_it_was_not_carried_out():
    """#2061, on a real encrypted pair: a verb with an EFFECT runs, and then its answer cannot
    go out (the byte budget is spent, the answer is over the cap, it cannot be encoded). The
    caller must not read a code that means "refused": every one of them is `answer_lost`, in one
    small frame. A refusal decided BEFORE a handler runs is still `busy`, with no effect."""
    effects: list[str] = []

    async def _deliver(channel, args):
        effects.append("delivered")
        return {"receipt": "sent"}

    async def _deliver_huge(channel, args):
        effects.append("delivered huge")
        return {"blob": "y" * (ch.ANSWER_MAX + 1)}

    async def _deliver_unsendable(channel, args):
        effects.append("delivered unsendable")
        return {"receipt": object()}

    verbs = {
        v.name: v
        for v in (
            ch.Verb("t.deliver", frozenset({ch.MODE_PEER}), _no_args, _deliver),
            ch.Verb("t.huge", frozenset({ch.MODE_PEER}), _no_args, _deliver_huge),
            ch.Verb("t.unsendable", frozenset({ch.MODE_PEER}), _no_args, _deliver_unsendable),
        )
    }

    async def code(a, verb) -> str:
        with pytest.raises(ch.CallError) as exc:
            await a.call(verb, timeout=5)
        assert exc.value.dispatched is True
        return exc.value.code

    async def go():
        a, b, _, b_end, tasks = await _connect(verbs=verbs)
        assert await a.call("t.deliver") == {"receipt": "sent"}
        # the minute's answer bytes are spent (far below zero: the refill does not rescue it)
        b._bytes_out._tokens = -1e12
        before = len(b_end.sent)
        assert await code(a, "t.deliver") == ch.ANSWER_LOST
        assert effects == ["delivered", "delivered"]  # it RAN: "busy" would have been a lie
        assert b.stats.busy == 1
        # what went out instead: the one fixed frame, 33 bytes of answer, not charged
        assert len(ch._ANSWER_LOST_BODY) == 33
        assert sum(len(w) for w in b_end.sent[before:]) < 256
        assert b._bytes_out._tokens <= -1e12 + 1e9
        b._bytes_out._tokens = b._bytes_out._cap
        assert await code(a, "t.huge") == ch.ANSWER_LOST
        assert await code(a, "t.unsendable") == ch.ANSWER_LOST
        assert effects[2:] == ["delivered huge", "delivered unsendable"]
        await _shutdown(a, b, tasks)

        # decided before any handler runs: `busy`, and nothing happened
        a, b, _, _, tasks = await _connect(verbs=verbs, budgets=ch.Budgets(calls_per_minute=1))
        await a.call("t.deliver")
        ran = len(effects)
        assert await code(a, "t.deliver") == "busy"
        assert len(effects) == ran
        await _shutdown(a, b, tasks)

    run(go())
