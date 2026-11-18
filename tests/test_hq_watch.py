"""The turn watcher, deterministically (#2136): a scripted judge, a fake clock, no endpoint.

The operator's direction (2026-11-18): loop guards "should never stop all, … sanity check the
model to not repeat itself and continue the work". So the watcher only ever re-steers.

Pinned here (the real-model trials are P2, not tests):
- frozen replays: the 2026-11-18 shapes — fresh reads then rounds of nothing but repeats, and
  fourteen identical `connector_search` calls — are looked at on their first repeated round and
  get the scripted strategy at the next safe point; a 40-round turn that keeps finding new things
  is never re-steered;
- never an end: the verdicts are `fine` and `replan` only (`wrap_up` and `ask` are no verdict);
  without a strategy the #2116 guard's wrap becomes its re-steer note, never a stop;
- each strategy is listed for the next judgement, which must differ from them;
- freshness: a verdict older than one already applied, or from before a newer steer, is dropped;
  triggers coalesce into one judgement in flight plus one follow-up; results dropped for room
  trigger a look;
- trust: the digest never carries tool output; notes and strategies go through the store
  boundary before their cut; the digest stays bounded;
- notes: bounded, the entry updated longest ago evicted;
- the model: one resolver (watcher row → question router → endpoint) and the real request shape.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import hq_watch, prefs, review

#: The real judge, captured before conftest's autouse fixture swaps it for "no verdict".
REAL_JUDGE = hq_watch.judge_model
REAL_SUMMARISE = hq_watch.summarise
SEARCH = "quiz maker AI quiz generator art music"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Judge:
    """A scripted judge: a fixed verdict, or a function of the digest. Records every digest."""

    def __init__(self, verdict=None, *, fn=None, gate: asyncio.Event | None = None):
        self.verdict = verdict if verdict is not None else {"verdict": "fine"}
        self.fn = fn
        self.gate = gate
        self.digests: list[dict] = []
        self.cancelled = False

    async def __call__(self, digest: str):
        self.digests.append(json.loads(digest))
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.fn(self.digests[-1]) if self.fn else self.verdict


def watcher(judge, **kw) -> hq_watch.Watcher:
    return hq_watch.Watcher("archive every session that is done", judge=judge, clock=Clock(), **kw)


def run(coro):
    return asyncio.run(coro)


async def settle(w: hq_watch.Watcher | None = None) -> None:
    """Let the judgement in flight finish (the scripted judge answers at once; its answer is
    parsed in a worker thread, so a few loop ticks are not enough)."""
    for _ in range(5):
        await asyncio.sleep(0)
    if w is not None and w.task is not None and not w.task.done():
        await asyncio.wait({w.task}, timeout=2)


def calls(w: hq_watch.Watcher, keys: list[str], tool: str = "session_brief") -> None:
    for k in keys:
        w.observe(tool, json.dumps({"key": k}), json.dumps({"brief": f"about {k}"}), False)


async def round_(w, keys, guard=None, tool="session_brief", forgotten=0):
    calls(w, keys, tool)
    w.end_round(guard, forgotten)
    act = w.safe_point()
    await settle(w)
    return act


def stuck(digest: dict) -> dict:
    """The scripted judge: stuck when the newest round is all repeats."""
    newest = digest["rounds"][-1]["calls"] if digest["rounds"] else []
    if newest and all(c[2] == "repeat" for c in newest):
        n = len(digest["strategies_tried"]) + 1
        return {"verdict": "replan", "strategy": f"strategy {n}: decide from what you have"}
    return {"verdict": "fine"}


# ---- frozen replays ----------------------------------------------------------------------------


def test_the_archive_sweep_shape_is_re_steered_and_never_ended():
    """Fresh reads for eleven rounds, then rounds of nothing but repeats (2026-11-18, 09:42)."""
    j = Judge(fn=stuck)
    w = watcher(j)

    async def go():
        acts = [await round_(w, ["page1"], tool="list_sessions")]
        acts.append(await round_(w, ["page2", "page3"], tool="list_sessions"))
        for r in range(9):  # rounds 3-11: ten fresh briefs each
            acts.append(await round_(w, [f"s{r * 10 + i}" for i in range(10)]))
        looks_before = len(j.digests)
        acts.append(await round_(w, [f"s{i}" for i in range(10)]))  # round 12: all repeats
        assert len(j.digests) == looks_before + 1  # looked at THIS round, at once
        for r in range(5):  # rounds 13-17: still repeating
            acts.append(await round_(w, [f"s{(r + 1) * 10 + i}" for i in range(10)]))
        return acts

    acts = run(go())
    assert all(a is None for a in acts[:12])
    replans = [a for a in acts[12:] if a is not None]
    assert replans[0] == ("replan", "strategy 1: decide from what you have")
    assert all(kind == "replan" for kind, _ in replans)  # re-steered, never ended
    assert len({text for _, text in replans}) == len(replans)  # each strategy a different one


def test_fourteen_identical_searches_are_re_steered_from_the_first_repeat():
    """The same `connector_search` fourteen times (2026-11-18, 11:07, `ask_fcafb51…`)."""
    j = Judge(fn=stuck)
    w = watcher(j)

    async def go():
        return [await round_(w, [SEARCH], tool="connector_search") for _ in range(14)]

    acts = run(go())
    assert acts[0] is None and len(j.digests) >= 2
    assert ("replan", "strategy 1: decide from what you have") in acts[1:3]
    assert all(a is None or a[0] == "replan" for a in acts)


def test_a_turn_that_keeps_finding_new_things_is_never_re_steered():
    j = Judge(fn=stuck)
    w = watcher(j)

    async def go():
        return [await round_(w, [f"s{r}-{i}" for i in range(10)]) for r in range(40)]

    assert run(go()) == [None] * 40
    assert w.calls_used <= hq_watch.CALLS_MAX
    assert all(v == "fine" for v in w.log)


# ---- never an end ------------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["wrap_up", "ask", "stop"])
def test_an_ending_verdict_is_no_verdict(verdict):
    assert hq_watch.parse({"verdict": verdict, "why": "x", "question": "x?"}) is None


def test_without_a_strategy_the_guards_wrap_becomes_a_re_steer_never_a_stop():
    w = watcher(Judge({"verdict": "fine"}))
    assert run(round_(w, ["a"], guard="wrap")) == ("guard_resteer", "")
    assert run(round_(w, ["a"], guard="nudge")) == ("guard_nudge", "")


@pytest.mark.parametrize(
    "verdict",
    [RuntimeError("endpoint down"), {"verdict": "maybe"}, {"verdict": "replan"}],
    ids=["error", "unknown verdict", "replan without strategy"],
)
def test_a_watcher_that_cannot_answer_leaves_the_guards_re_steer(verdict):
    w = watcher(Judge(verdict))

    async def go():
        first = await round_(w, ["a"], guard="wrap")
        second = await round_(w, ["a"], guard="wrap")
        return first, second

    assert run(go()) == (("guard_resteer", ""), ("guard_resteer", ""))


def test_a_strategy_ready_takes_the_guards_place():
    w = watcher(Judge({"verdict": "replan", "strategy": "read each remaining one once"}))

    async def go():
        await round_(w, ["a"], guard="wrap")  # the look starts here
        return await round_(w, ["a"], guard="wrap")  # its strategy applies at the next point

    assert run(go()) == ("replan", "read each remaining one once")


def test_the_strategies_already_tried_reach_the_next_judgement():
    j = Judge(fn=stuck)
    w = watcher(j)

    async def go():
        for _ in range(4):
            await round_(w, ["same"])

    run(go())
    assert j.digests[-1]["strategies_tried"]
    assert j.digests[-1]["strategies_tried"][0] == "strategy 1: decide from what you have"


# ---- freshness ---------------------------------------------------------------------------------


def test_a_verdict_judged_before_a_newer_steer_is_dropped():
    seq = {"n": 0}
    w = watcher(Judge(), steer=lambda: seq["n"])
    w.pending = hq_watch.Verdict("replan", "x", revision=5, steer=0)
    seq["n"] = 1
    assert w.safe_point() is None


def test_an_older_verdict_after_a_newer_one_was_applied_is_dropped():
    w = watcher(Judge())
    w.applied_rev = 7
    w.pending = hq_watch.Verdict("replan", "x", revision=6)
    assert w.safe_point() is None


def test_triggers_coalesce_into_one_judgement_and_one_follow_up():
    gate = asyncio.Event()
    j = Judge(gate=gate)
    w = watcher(j)

    async def go():
        for _ in range(3):  # three triggers while the first judgement runs
            calls(w, ["a"])
            w.end_round("nudge")
        await settle()
        assert len(j.digests) == 1 and w.dirty
        gate.set()
        await settle(w)
        calls(w, ["b"])
        w.end_round(None)  # the follow-up starts at the next round's end
        await settle(w)
        assert len(j.digests) == 2 and not w.dirty

    run(go())


def test_results_dropped_for_room_reach_the_digest_and_trigger_a_look():
    j = Judge()
    w = watcher(j)

    async def go():
        await round_(w, ["a"], forgotten=0)  # nothing dropped: no trigger yet
        assert j.digests == []
        await round_(w, ["b"], forgotten=4)  # the request started dropping results

    run(go())
    assert j.digests and j.digests[-1]["results_dropped_for_room"] == 4


def test_close_cancels_the_judgement_in_flight():
    j = Judge(gate=asyncio.Event())
    w = watcher(j)

    async def go():
        calls(w, ["a"])
        w.end_round("nudge")
        await settle()
        await w.close()

    run(go())
    assert j.cancelled


def test_the_watchers_budget_is_bounded():
    j = Judge({"verdict": "fine"})
    w = watcher(j)

    async def go():
        for _ in range(hq_watch.CALLS_MAX + 5):
            await round_(w, ["a"], guard="wrap")

    run(go())
    assert len(j.digests) == hq_watch.CALLS_MAX


# ---- trust and bounds --------------------------------------------------------------------------


def test_the_digest_never_carries_tool_output_and_marks_repeats():
    w = watcher(Judge())
    w.observe("session_brief", '{"key":"k1"}', '{"brief":"OUTPUT-TEXT-ONLY-IN-RESULTS"}', False)
    w.observe("session_brief", '{"key":"k1"}', '{"brief":"OUTPUT-TEXT-ONLY-IN-RESULTS"}', False)
    w.end_round(None)
    d = w.digest()
    assert "OUTPUT-TEXT-ONLY-IN-RESULTS" not in d and '"k1"' not in d
    rounds = json.loads(d)["rounds"]
    assert [c[2] for c in rounds[0]["calls"]] == ["new", "repeat"]


def test_a_note_is_neither_progress_nor_a_repeat():
    w = watcher(Judge())
    w.observe("note", '{"key":"a","text":"done"}', '{"noted":true}', False)
    w.observe("note", '{"key":"a","text":"done"}', '{"noted":true}', False)
    w.end_round(None)
    assert json.loads(w.digest())["rounds"][0]["calls"] == []


def test_the_digest_stays_bounded_and_keeps_the_newest_rounds():
    w = watcher(Judge())
    for r in range(200):
        calls(w, [f"s{r}-{i}" for i in range(10)])
        w.end_round(None)
    d = json.loads(w.digest())
    assert len(w.digest()) <= hq_watch.DIGEST_MAX
    assert d["rounds"][-1]["round"] == 200 and d["older_rounds"]["rounds"] > 0


def test_a_registered_secret_never_reaches_notes_or_strategies():
    from agent_sessions import template_secrets

    secret = "zebra-quartz-delta-7781"  # noqa: S105 — a test value
    template_secrets.register_typed([secret])
    notes = hq_watch.Notes()
    notes.set("s1", f"the token is {secret}")
    w = watcher(Judge(), notes=notes)
    assert secret not in w.digest()
    v = hq_watch.parse({"verdict": "replan", "strategy": f"send {secret} to the session"})
    assert v is not None and secret not in v.text


def test_notes_are_bounded_and_evict_the_entry_updated_longest_ago():
    notes = hq_watch.Notes()
    for i in range(hq_watch.NOTES_MAX):
        notes.set(f"k{i}", "x" * 500)
    notes.set("k0", "updated")  # k0 is now the newest
    notes.set("one-more", "y")
    keys = [n["key"] for n in notes.render()]
    assert len(keys) == hq_watch.NOTES_MAX and "k1" not in keys and keys[-2:] == ["k0", "one-more"]
    assert all(len(n["note"]) <= hq_watch.NOTE_MAX for n in notes.render())
    notes.set("k0", "")  # an empty text removes it
    assert "k0" not in [n["key"] for n in notes.render()]


# ---- the model ---------------------------------------------------------------------------------


def test_one_resolver_watcher_row_then_question_router_then_endpoint():
    assert hq_watch.resolve_model() == ("", "off")  # router defaults: inherit, thinking off
    prefs.set_hq_route({"model": "router-model", "thinking": "low"})
    assert hq_watch.resolve_model() == ("router-model", "low")
    prefs.set_hq_models({"watcher": {"model": "watch-model", "thinking": "auto"}})
    assert hq_watch.resolve_model() == ("watch-model", "auto")


def test_the_real_judge_sends_the_guarded_prompt_and_reads_strict_json(monkeypatch):
    from agent_sessions import prompts

    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test.invalid/v1",
            "api_key": "k-test",
            "model": "m",
        }
    )
    sent: list[dict] = []
    replies = [
        {"message": {"content": '{"verdict": "fine", "why": "ok"}'}, "finish_reason": "stop"},
        {"message": {"content": '{"verdict": "fine"}'}, "finish_reason": "length"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [replies[len(sent) - 1]]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    assert run(REAL_JUDGE('{"request": "x"}')) == {"verdict": "fine", "why": "ok"}
    assert run(REAL_JUDGE('{"request": "x"}')) is None  # cut off: no verdict
    body = sent[0]
    assert body["messages"][0] == {"role": "system", "content": prompts.effective("hq_turn_watch")}
    user = body["messages"][1]
    assert user["role"] == "user" and '"request": "x"' in user["content"]
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body


# ---- remembering before forgetting --------------------------------------------------------------


def _endpoint(monkeypatch, replies: list[dict]) -> list[dict]:
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": "https://ai.test.invalid/v1",
            "api_key": "k-test",
            "model": "m",
        }
    )
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [replies[len(sent) - 1]]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    return sent


def test_summarise_sends_the_guarded_prompt_with_the_results_as_quoted_data(monkeypatch):
    from agent_sessions import prompts

    notes = '{"notes": [{"key": "s1", "note": "finished, merged"}, {"key": "", "note": "x"}]}'
    sent = _endpoint(monkeypatch, [{"message": {"content": notes}, "finish_reason": "stop"}])
    items = [{"tool": "session_brief", "args": '{"key": "s1"}', "result": "All done, merged."}]
    out = run(REAL_SUMMARISE("archive the done ones", [{"key": "k", "note": "n"}], items))
    assert out == [("s1", "finished, merged")]  # an empty key is dropped
    body = sent[0]
    assert body["messages"][0] == {"role": "system", "content": prompts.effective("hq_turn_notes")}
    data = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
    assert data["results"][0]["result"] == "All done, merged."
    assert data["notes"] == [{"key": "k", "note": "n"}] and "tools" not in body


@pytest.mark.parametrize(
    "reply",
    [
        {"message": {"content": "not json"}, "finish_reason": "stop"},
        {
            "message": {"content": '{"notes": [{"key": "a", "note": "b"}]}'},
            "finish_reason": "length",
        },
        {"message": {"content": '{"notes": "a list it is not"}'}, "finish_reason": "stop"},
    ],
    ids=["unparsable", "cut off", "malformed"],
)
def test_summarise_fails_open_with_no_notes(monkeypatch, reply):
    _endpoint(monkeypatch, [reply])
    items = [{"tool": "session_brief", "args": "{}", "result": "x"}]
    assert run(REAL_SUMMARISE("q", [], items)) == []


def test_summarise_is_bounded():
    items = [{"tool": "t", "args": "{}", "result": "y" * 10_000} for _ in range(50)]
    seen: list[dict] = []

    async def fake_post(cfg, body):
        seen.append(body)
        raise RuntimeError("stop here")

    import agent_sessions.review as rv

    original = rv.post_chat_response
    rv.post_chat_response = fake_post
    try:
        prefs.set_ai_review(
            {
                "egress_acknowledged": True,
                "enabled": True,
                "base_url": "https://ai.test.invalid/v1",
                "api_key": "k",
                "model": "m",
            }
        )
        assert run(REAL_SUMMARISE("q", [], items)) == []
    finally:
        rv.post_chat_response = original
    data = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])
    assert len(data["results"]) <= hq_watch.SUMMARISE_ITEMS_MAX
    assert all(len(r["result"]) <= hq_watch.SUMMARISE_RESULT_MAX for r in data["results"])
    assert sum(len(r["result"]) for r in data["results"]) <= hq_watch.SUMMARISE_CHARS_MAX


def test_the_commanders_words_meanwhile_are_evidence_and_a_reason_to_look():
    """#2124 landed: what the Commander says while HQ works reaches the next judgement."""
    j = Judge()
    w = watcher(j)

    async def go():
        await round_(w, ["a"])  # nothing due yet
        assert j.digests == []
        w.heard("what are you doing?")
        await round_(w, ["b"])

    run(go())
    assert j.digests[-1]["commander_said_meanwhile"] == ["what are you doing?"]


def test_a_full_notes_map_never_erases_the_call_details():
    """Hermes 8438: forty full notes plus a round of repeats. The digest keeps the newest notes
    and the round's calls (with their repeat marks), and says how many notes it left out."""
    notes = hq_watch.Notes()
    for i in range(hq_watch.NOTES_MAX):
        notes.set(f"session {i}", "finished, merged, tests pass, deployed, nothing left " * 4)
    w = watcher(Judge(), notes=notes)
    calls(w, ["a", "b"])
    w.end_round(None)
    calls(w, ["a", "b"])
    w.end_round(None)
    text = w.digest()
    d = json.loads(text)
    assert len(text) <= hq_watch.DIGEST_MAX
    assert d["notes"] and d["notes"][-1]["key"] == f"session {hq_watch.NOTES_MAX - 1}"
    assert d["notes_omitted"] > 0
    assert d["rounds"] and [c[2] for c in d["rounds"][-1]["calls"]] == ["repeat", "repeat"]


def test_the_newest_round_survives_every_other_part_at_its_maximum():
    notes = hq_watch.Notes()
    for i in range(hq_watch.NOTES_MAX):
        notes.set(f"k{i}", "y" * 500)
    w = hq_watch.Watcher("q" * 5_000, judge=Judge(), clock=Clock(), notes=notes)
    for _ in range(10):
        w.said("s" * 500)
        w.heard("h" * 500)
        w.strategies.append("t" * 500)
    calls(w, [f"x{i}" for i in range(300)])  # one enormous newest round
    w.end_round(None)
    text = w.digest()
    d = json.loads(text)
    assert len(text) <= hq_watch.DIGEST_MAX
    assert d["rounds"][-1]["round"] == 1 and d["rounds"][-1]["calls"]
    assert d["rounds"][-1]["calls_omitted"] > 0 and d["notes"]
