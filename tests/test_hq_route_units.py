"""HQ's question router (#2028): the request-thinking override and the honest search count."""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import ai_providers, pulse_chat

OPENAI = {"provider": "openai", "thinking": "high"}


def test_the_override_sets_thinking_for_requests_inside_it_only():
    assert ai_providers.thinking_fields(OPENAI, interactive=True) == {"reasoning_effort": "high"}
    with ai_providers.request_thinking("off"):
        assert ai_providers.thinking_fields(OPENAI, interactive=True) == {
            "reasoning_effort": "minimal"
        }
    assert ai_providers.thinking_fields(OPENAI, interactive=True) == {"reasoning_effort": "high"}


def test_none_or_an_unknown_level_keeps_the_turns_own_level():
    for level in (None, "hq", "bogus"):
        with ai_providers.request_thinking(level):
            assert ai_providers.thinking_fields(OPENAI, interactive=True) == {
                "reasoning_effort": "high"
            }


def test_the_override_beats_a_spoken_turns_level_and_restores_it():
    with ai_providers.spoken_turn(thinking="medium"):
        assert ai_providers.thinking_fields(OPENAI, interactive=True) == {
            "reasoning_effort": "medium"
        }
        with ai_providers.request_thinking("low"):
            assert ai_providers.thinking_fields(OPENAI, interactive=True) == {
                "reasoning_effort": "low"
            }
            assert ai_providers.in_spoken_turn()  # routing never changes what a spoken turn is
        assert ai_providers.thinking_fields(OPENAI, interactive=True) == {
            "reasoning_effort": "medium"
        }


def test_the_override_is_restored_after_an_error():
    with pytest.raises(RuntimeError), ai_providers.request_thinking("off"):
        raise RuntimeError("boom")
    assert ai_providers.thinking_fields(OPENAI, interactive=True) == {"reasoning_effort": "high"}


def test_concurrent_turns_never_see_each_others_override():
    seen: dict[str, dict] = {}

    async def turn(name: str, level: str | None, gate: asyncio.Event, other: asyncio.Event):
        with ai_providers.request_thinking(level):
            other.set()
            await gate.wait()  # both are inside their blocks at once
            seen[name] = ai_providers.thinking_fields(OPENAI, interactive=True)

    async def main():
        a, b = asyncio.Event(), asyncio.Event()
        both = asyncio.Event()

        async def release():
            await a.wait()
            await b.wait()
            both.set()

        await asyncio.gather(
            turn("typed", None, both, a),
            turn("router", "off", both, b),
            release(),
        )

    asyncio.run(main())
    assert seen == {
        "typed": {"reasoning_effort": "high"},
        "router": {"reasoning_effort": "minimal"},
    }


def _card(i: int, title: str) -> dict:
    return {"id": f"claude:{i:08d}-0000-4000-8000-000000000000", "title": title}


def test_hits_count_only_keyword_matches_in_the_slice():
    catalog = [_card(i, "login bug" if i < 3 else "other work") for i in range(200)]
    slice_, hits = pulse_chat._prefilter_hits(catalog, "the login bug")
    assert len(slice_) == pulse_chat.CATALOG_SLICE_MAX and hits == 3


def test_zero_hits_is_recency_padding_only():
    catalog = [_card(i, "other work") for i in range(10)]
    slice_, hits = pulse_chat._prefilter_hits(catalog, "what model are you")
    assert len(slice_) == 10 and hits == 0


def test_hits_above_the_cap_are_bounded_by_the_cap():
    catalog = [_card(i, "login bug") for i in range(200)]
    slice_, hits = pulse_chat._prefilter_hits(catalog, "login")
    assert hits == len(slice_) == pulse_chat.CATALOG_SLICE_MAX


def test_pinned_model_is_restored_after_an_error_and_scoped_per_task():
    with pytest.raises(RuntimeError), ai_providers.pinned_model():
        assert ai_providers.model_pinned()
        raise RuntimeError("boom")
    assert not ai_providers.model_pinned()

    async def main():
        seen = {}
        gate = asyncio.Event()

        async def pinned():
            with ai_providers.pinned_model():
                await gate.wait()
                seen["pinned"] = ai_providers.model_pinned()

        async def plain():
            gate.set()
            seen["plain"] = ai_providers.model_pinned()

        await asyncio.gather(pinned(), plain())
        return seen

    assert asyncio.run(main()) == {"pinned": True, "plain": False}
