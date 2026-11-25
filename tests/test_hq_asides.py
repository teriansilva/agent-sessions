"""#2113 P2 — HQ thinks out loud on a call.

What is pinned here (the call side's cadence is in `test_voice_progress.py`):

* `ask_orchestrator.aside_text` puts the WHOLE text through the store's text boundary
  (`ask_store.guard_text`) before cutting it to one line of at most `ASIDE_MAX` characters: a
  registered secret across the cut is never said in part, a credential-shaped value is withheld,
  and no redaction (the secret store unreadable) says nothing;
* a CALL turn publishes the model's clean text beside a round's tool calls as one
  ``{"type": "aside"}`` event — never a progress step — and a typed turn publishes none;
* leaked call markup is never an aside.
"""

from __future__ import annotations

import uuid

import pytest

from agent_sessions import ask_orchestrator, ask_store, template_secrets
from test_ask_draft_turns import run  # noqa: F401
from test_ask_orchestrator import CRED, ai, answer  # noqa: F401 — fixtures are used by name

SECRET = "zq-PLANTED-aside-secret-7Hk2Lm9Qp4"


def _with_text(text: str, name: str = "list_projects") -> dict:
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }


# ---- the text boundary -----------------------------------------------------------------------


def test_an_aside_is_one_plain_line():
    said = ask_orchestrator.aside_text("**Let me look** in `Forgejo` for that.\nSecond line.")
    assert said == "Let me look in Forgejo for that."


def test_a_long_aside_is_cut_at_a_sentence_or_a_word():
    first = "Let me look in Forgejo for the federation epic first. "
    said = ask_orchestrator.aside_text(first + "x " * 200)
    assert said == first.strip()
    said = ask_orchestrator.aside_text("word " * 100)
    assert len(said) <= ask_orchestrator.ASIDE_MAX and said.endswith("…")


def test_a_registered_secret_across_the_cut_is_never_said_in_part():
    template_secrets.register_typed([SECRET])
    pad = "a" * (ask_orchestrator.ASIDE_MAX - 10)  # the secret straddles the 160-character cut
    said = ask_orchestrator.aside_text(f"{pad} {SECRET} and more")
    assert said is not None
    assert not any(SECRET[:k] in said for k in range(4, len(SECRET) + 1))


def test_a_credential_shaped_aside_is_withheld():
    assert ask_orchestrator.aside_text(f"Trying it with {CRED} now.") is None


def test_no_redaction_says_nothing(monkeypatch):
    def unavailable(*a, **k):
        raise template_secrets.RedactionUnavailable("secret store unreadable")

    monkeypatch.setattr(template_secrets, "redact_text", unavailable)
    assert ask_orchestrator.aside_text("Let me look.") is None


@pytest.mark.parametrize("text", [None, "", "   ", "\n\n"])
def test_nothing_to_say(text):
    assert ask_orchestrator.aside_text(text) is None


# ---- the event -------------------------------------------------------------------------------


def _events(via: str | None) -> list[dict]:
    async def scenario():
        cid = ask_store.create()
        task, view = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "where is the epic", working_keys=set(), via=via
        )
        assert task is not None and view is None
        queue = ask_orchestrator.listen(cid, task)
        await task
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return run(scenario())


def test_a_call_turn_publishes_its_aside_and_no_step_for_it(ai):  # noqa: F811
    ai(_with_text("Let me look in Forgejo for that."), answer("Found it."))
    events = _events("call")
    asides = [e for e in events if e.get("type") == "aside"]
    assert asides == [{"type": "aside", "text": "Let me look in Forgejo for that."}]
    assert all(e.get("step") != "aside" for e in events)


def test_a_typed_turn_publishes_no_aside(ai):  # noqa: F811
    ai(_with_text("Let me look in Forgejo for that."), answer("Found it."))
    assert not [e for e in _events(None) if e.get("type") == "aside"]


def test_leaked_call_markup_is_never_an_aside(ai):  # noqa: F811
    ai(_with_text('<tool_call>{"name": "list_projects"}</tool_call>'), answer("ok"))
    assert not [e for e in _events("call") if e.get("type") == "aside"]
