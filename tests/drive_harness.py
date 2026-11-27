"""Shared fixtures for HQ driving API sessions (#2397): a real native API session over the fake
native child (`host`), HQ's conversation over the scripted AI endpoint (`ai`), and the catalog HQ's
find tool answers from."""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import time

import pytest

from agent_sessions import ask_orchestrator, ask_proposals, ask_store, pulse_chat, session_driver
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, frames, ident

ENGINE = ENGINES["codex"]


def users(work) -> list[str]:
    """The texts the agent was sent (codex `turn/start` inputs), as the fake child logged them."""
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("method") != "turn/start":
            continue
        for item in (frame.get("params") or {}).get("input") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out.append(item["text"])
    return out


def card_row(key: str, engine: str = ENGINE) -> dict:
    return {
        "id": key,
        "engine": engine,
        "title": "web shop strings",
        "cwd": "/work/web-shop",
        "project": {"kind": "folder", "id": "p1", "name": "web-shop"},
        "state": "idle",
        "last_activity": time.time() - 3600,
    }


@pytest.fixture
def fleet(monkeypatch):
    """HQ's find and catalog see exactly the sessions in ``state["cards"]``."""
    state: dict = {"cards": []}

    async def fake_ask(query, history=None, *, working_keys=None, on_event=None, scope=None, **_):
        return {
            "answer": "found",
            "matches": [
                {"id": c["id"], "title": c["title"], "state": "idle", "why": "it"}
                for c in state["cards"]
            ],
            "mission_matches": [],
            "stage": "catalog",
            "configured": True,
        }

    async def catalog(self):
        return [dict(c) for c in state["cards"]]

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    monkeypatch.setattr(ask_orchestrator._Turn, "catalog", catalog)
    return state


def new_session(project) -> str:
    """A live API session, listed the way the sidebar lists it (its store kind attached: the
    fixture provider has none of its own)."""
    from agent_sessions import chat_store, engines

    prov = engines.get(ENGINE)
    if prov.kind is None:
        prov.attach_kind(chat_store.ChatStoreKind())
    key = asyncio.run(runtime.create_session(ENGINE, str(project), operation_id=ident()))[
        "session_key"
    ]
    engines.invalidate_scan_cache()
    return key


def drive_from(key: str, cid: str, title: str = "web shop strings") -> dict:
    """HQ drives ``key`` from conversation ``cid`` (what its start card records)."""
    rec = session_driver.record_start(key, cid, title)
    assert rec is not None
    return rec


def result(script, call_id: str) -> dict:
    """The tool result the model got for its call ``call_id``."""
    for body in reversed(script.bodies):
        for m in body.get("messages") or []:
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
                return json.loads(m["content"])
    raise AssertionError(f"no result for {call_id}")


def cards(cid: str, op: str | None = None) -> list[dict]:
    rows = [r for r in ask_store.read_proposals(cid) if r.get("kind") == "operation"]
    return [r for r in rows if op is None or r.get("op") == op]


def card(cid: str, op: str) -> dict:
    (row,) = cards(cid, op)
    return ask_proposals._row(cid, row["id"]) or row
