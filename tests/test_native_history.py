"""Scrolling up past an API session's snapshot window (#1504): pages of older turns, keyed by
turn id, shaped as the snapshot's are, read one render at a time."""

import asyncio
import uuid

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, structured_runtime
from test_native_agent_turns import T0, agent, operator, raw

conversation = journal_tests.conversation
CURSOR = "3f1c2b4a-5d6e-4f70-8a91-b2c3d4e5f607"


def page(root, sid, binding, before, limit):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id,
        max_turns=limit,
        before=before,
    )


def _seven(root, sid, binding, monkeypatch):
    """Seven turns, both kinds interleaved, so a page boundary can fall between them."""
    ids = []
    for i in range(7):
        if i in (2, 5):
            ids.append(agent(root, sid, binding, monkeypatch, T0 + i, f"own {i}"))
        else:
            ids.append(operator(root, sid, binding, monkeypatch, T0 + i, f"op {i}"))
    return ids


def test_pages_walk_back_from_the_window_to_the_first_turn(conversation, monkeypatch):
    root, sid, binding = conversation
    ids = _seven(root, sid, binding, monkeypatch)
    full = raw(root, sid, binding)["turns"]
    window = raw(root, sid, binding, max_turns=3)
    assert [t["turn_id"] for t in window["turns"]] == ids[4:]
    assert window["history_before"] == ids[4]
    walked, cursor = list(window["turns"]), window["history_before"]
    pages = 0
    while cursor is not None:
        older = page(root, sid, binding, cursor, 3)
        assert older["turns"], "a cursor with older turns returns some"
        walked[:0] = older["turns"]
        cursor = older["history_before"]
        pages += 1
    assert pages == 2  # ids[1:4], then ids[0:1]
    # Chronological, every turn once, each exactly as the full projection shows it.
    assert walked == full


def test_a_window_holding_everything_has_nowhere_to_go(conversation, monkeypatch):
    root, sid, binding = conversation
    ids = _seven(root, sid, binding, monkeypatch)
    assert raw(root, sid, binding, max_turns=7)["history_before"] is None
    assert raw(root, sid, binding, max_turns=6)["history_before"] == ids[1]
    assert page(root, sid, binding, ids[1], 5)["history_before"] is None


def test_the_cursor_holds_while_new_turns_arrive(conversation, monkeypatch):
    root, sid, binding = conversation
    ids = _seven(root, sid, binding, monkeypatch)
    before = page(root, sid, binding, ids[4], 2)
    operator(root, sid, binding, monkeypatch, T0 + 10, "later")
    agent(root, sid, binding, monkeypatch, T0 + 11, "later own")
    after = page(root, sid, binding, ids[4], 2)
    assert after["turns"] == before["turns"]
    assert [t["turn_id"] for t in after["turns"]] == ids[2:4]
    assert after["history_before"] == before["history_before"] == ids[2]


def test_an_unknown_cursor_is_refused_not_guessed(conversation, monkeypatch):
    root, sid, binding = conversation
    _seven(root, sid, binding, monkeypatch)
    with pytest.raises(native_runtime.NativeError) as e:
        page(root, sid, binding, str(uuid.uuid4()), 3)
    assert e.value.status == 409 and "cursor" in e.value.detail


def test_a_removed_message_is_never_a_page_turn_or_a_cursor(conversation, monkeypatch):
    from test_native_withdraw import _claim, _withdraw

    root, sid, binding = conversation
    first = operator(root, sid, binding, monkeypatch, T0, "first")
    gone = _claim(root, sid, binding, queued=True)
    _withdraw(root, sid, binding, gone)
    last = operator(root, sid, binding, monkeypatch, T0 + 2, "last")
    older = page(root, sid, binding, last, 5)
    assert [t["turn_id"] for t in older["turns"]] == [first]
    assert older["history_before"] is None
    with pytest.raises(native_runtime.NativeError) as e:
        page(root, sid, binding, gone, 5)
    assert e.value.status == 409


# ---- the facade: validation, shaping, one render at a time ------------------------------------


class _Pages:
    """A native adapter whose history call blocks until released, recording what it was asked."""

    def __init__(self, turns, history_before):
        self.turns, self.history_before = turns, history_before
        self.calls: list[tuple] = []
        self.release = asyncio.Event()

    async def history(self, engine, native, before, limit):
        self.calls.append((engine, native, before, limit))
        await self.release.wait()
        return {"turns": self.turns, "history_before": self.history_before}


def _target(monkeypatch, adapter):
    monkeypatch.setattr(
        structured_runtime,
        "_observation_target",
        lambda key, op: (*key.split(":", 1), adapter, None),
    )


def _operator_turn(turn_id, text):
    return {"turn_id": turn_id, "status": "done", "text": text, "reply": text, "ts": T0}


@pytest.mark.parametrize(
    ("before", "limit"),
    # Fixed ids: a random one would differ per xdist worker, and collection would not match.
    [("not-a-uuid", 5), (None, 5), (CURSOR, 0), (CURSOR, 51), (CURSOR, "5")],
)
def test_a_malformed_page_request_is_refused_before_any_read(monkeypatch, before, limit):
    adapter = _Pages([], None)
    adapter.release.set()  # a read that got through answers, never hangs the test
    _target(monkeypatch, adapter)
    with pytest.raises(structured_runtime.StructuredError) as e:
        asyncio.run(structured_runtime.history("codex-api:s", before=before, limit=limit))
    assert e.value.status == 422 and adapter.calls == []


def test_a_page_is_shaped_like_the_snapshot_under_one_budget_spent_newest_first(monkeypatch):
    monkeypatch.setattr(structured_runtime, "SNAPSHOT_TEXT_BUDGET", 30)
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    adapter = _Pages([_operator_turn(old, "o" * 20), _operator_turn(new, "n" * 20)], old)
    adapter.release.set()
    _target(monkeypatch, adapter)
    out = asyncio.run(structured_runtime.history("codex-api:s", before=new, limit=2))
    assert [t["turn_id"] for t in out["turns"]] == [old, new]
    # Spent newest first: the newest turn's text reads in full, the older one's is cut.
    assert out["turns"][1]["text"] == "n" * 20 and not out["turns"][1]["text_truncated"]
    assert out["turns"][0]["text_truncated"] or out["turns"][0]["reply_truncated"]
    assert out["before"] == old and out["has_more"] is True
    assert out["session_key"] == "codex-api:s"


def test_the_first_page_says_so(monkeypatch):
    first = str(uuid.uuid4())
    adapter = _Pages([_operator_turn(first, "hi")], None)
    adapter.release.set()
    _target(monkeypatch, adapter)
    out = asyncio.run(structured_runtime.history("codex-api:s", before=str(uuid.uuid4()), limit=50))
    assert out["before"] is None and out["has_more"] is False


def test_one_page_render_per_session_at_a_time(monkeypatch):
    adapter = _Pages([], None)
    _target(monkeypatch, adapter)

    async def scenario():
        first = asyncio.create_task(
            structured_runtime.history("codex-api:s", before=str(uuid.uuid4()), limit=5)
        )
        while not adapter.calls:
            await asyncio.sleep(0)
        with pytest.raises(structured_runtime.StructuredError) as e:
            # Bounded: without the guard this read would wait on the held one, not refuse.
            await asyncio.wait_for(
                structured_runtime.history("codex-api:s", before=str(uuid.uuid4()), limit=5), 5
            )
        assert e.value.status == 429
        # Another session is not held up by this one.
        other = asyncio.create_task(
            structured_runtime.history("codex-api:t", before=str(uuid.uuid4()), limit=5)
        )
        while len(adapter.calls) < 2:
            await asyncio.sleep(0)
        adapter.release.set()
        await first, await other
        # Released when it settles: the next page reads.
        await structured_runtime.history("codex-api:s", before=str(uuid.uuid4()), limit=5)

    asyncio.run(scenario())
    assert len(adapter.calls) == 3


def test_a_failed_read_releases_the_session(monkeypatch):
    class Broken:
        async def history(self, *_a):
            raise structured_runtime.StructuredError(409, "history cursor not found")

    _target(monkeypatch, Broken())
    for _ in range(2):  # the second is refused for its own reason, never 429
        with pytest.raises(structured_runtime.StructuredError) as e:
            asyncio.run(
                structured_runtime.history("codex-api:s", before=str(uuid.uuid4()), limit=5)
            )
        assert e.value.status == 409


def test_only_native_clients_offer_history():
    assert "history" in structured_runtime._NativeAdapter.operations(_Prov("codex-app-server"))
    assert not hasattr(structured_runtime._ChatAdapter, "history")


class _Prov:
    def __init__(self, kind):
        self.manifest = type("M", (), {"api": type("A", (), {"kind": kind})()})()
