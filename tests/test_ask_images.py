"""HQ sees shared images (#1779) — the current turn's pixels reach the model, honestly.

Pinned here, one acceptance item per test:

* an image shared with a question rides HQ's own request as an OpenAI ``image_url`` data part
  with the MIME sniffed from its bytes, and its note says it is attached;
* a text-only turn's request is unchanged (a plain string, no parts);
* an endpoint that refuses the image by name is asked ONCE more without it, the note saying it
  was not shown — and later turns go note-only straight away; a 413 or one bad picture retries
  but is not remembered; auth, rate-limit and unrelated errors are never retried or masked —
  the refusal is read from the endpoint's own message, never an echoed request or the model's
  name; the memo follows the model Auto actually sends;
* oversized, over-the-turn-budget, missing and mis-sniffed images degrade to the not-shown note;
* the parts ride every tool round of their turn, never a later turn's history, never the store,
  and never the fallback find or the call-end judgement (which say "not shown").
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from agent_sessions import ai_providers, ask_attachments, ask_orchestrator, ask_store, prefs, review
from test_ask_orchestrator import BASE, KEY, ai, answer, app, call, found  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x02" * 20
LOG = b"2026-10-26T09:12:04Z ERROR migration 3/7: relation does not exist\n"


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.setattr(ask_orchestrator, "_IMAGES_REFUSED", set())


def _publish(cid: str, name: str, data: bytes) -> dict:
    return ask_attachments.publish(cid, ask_attachments.new_aid(), name, data)


def _question(body: dict):
    """The current question's content in one request: the LAST user message."""
    return [m for m in body["messages"] if m.get("role") == "user"][-1]["content"]


def _images(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [p["image_url"]["url"] for p in content if p.get("type") == "image_url"]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p["text"] for p in content if p.get("type") == "text")


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _refusal(status: int, msg: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": msg}})


class _NoVision:
    """An endpoint whose model cannot take images: EVERY request carrying an image part gets
    ``refusal`` (the transport's own degrade retries included, as a real endpoint would); the
    rest get ``answers`` in order."""

    def __init__(self, refusal: httpx.Response, *answers: str):
        self.refusal, self.answers = refusal, list(answers)
        self.bodies: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if _images(_question(body)):
            return httpx.Response(self.refusal.status_code, content=self.refusal.content)
        text = self.answers.pop(0)
        msg = {"role": "assistant", "content": text}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})


@pytest.fixture
def no_vision(ai, monkeypatch):
    ai(answer("unused"))  # the configured endpoint

    def install(refusal: httpx.Response, *answers: str) -> _NoVision:
        endpoint = _NoVision(refusal, *answers)
        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(endpoint.handler))
        return endpoint

    return install


# ---- shown ------------------------------------------------------------------------------------


def test_a_shared_image_reaches_the_model_as_pixels(app, ai):
    script = ai(answer("a rack screen"))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    jpg = _publish(d, "rack.jpg", JPEG)
    turn = app.ask(d, "What do u see?", attachments=[png["id"], jpg["id"]]).json()["turn"]
    assert turn["status"] == "done" and turn["answer"] == "a rack screen"
    content = _question(script.bodies[0])
    # Text first, then one part per image in the order they were shared, MIME from the bytes.
    assert content[0]["type"] == "text"
    assert _images(content) == [_data_url("image/png", PNG), _data_url("image/jpeg", JPEG)]
    text = _text(content)
    assert "What do u see?" in text and "shot.png" in text and "rack.jpg" in text
    assert text.count(ask_attachments.IMAGE_SHOWN) == 2
    assert ask_attachments.IMAGE_NOT_SHOWN not in text


def test_a_text_only_turn_is_still_a_plain_string(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    app.ask(d, "hello there")
    assert _question(script.bodies[0]) == "hello there"


def test_a_text_file_beside_an_image_keeps_its_excerpt_and_is_never_a_part(app, ai):
    script = ai(answer("ok"))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    log = _publish(d, "deploy.log", LOG)
    app.ask(d, "both?", attachments=[png["id"], log["id"]])
    content = _question(script.bodies[0])
    assert len(_images(content)) == 1
    assert "relation does not exist" in _text(content)


def test_the_image_rides_every_tool_round_of_its_turn(app, ai):
    script = ai(call("list_missions"), answer("ok"))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    app.ask(d, "look", attachments=[png["id"]])
    assert len(script.bodies) == 2
    assert _images(_question(script.bodies[0])) == _images(_question(script.bodies[1])) != []


def test_a_later_turn_replays_the_note_never_the_image(app, ai):
    script = ai(answer("first"), answer("second"))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    app.ask(d, "look", attachments=[png["id"]])
    app.ask(d, "and now?")
    second = script.bodies[1]
    assert all(isinstance(m.get("content"), str | None) for m in second["messages"])
    replay = "\n".join(str(m.get("content")) for m in second["messages"])
    assert "shot.png" in replay  # the file existed
    assert base64.b64encode(PNG).decode() not in replay  # its pixels are not sent again


def test_the_store_never_holds_the_pixels(app, ai):
    ai(answer("ok"))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    app.ask(d, "look", attachments=[png["id"]])
    raw = (ask_store.root() / f"{d}.jsonl").read_text()
    assert png["id"] in raw
    assert base64.b64encode(PNG).decode() not in raw and "image_url" not in raw


# ---- refused ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,msg",
    [
        (400, "Invalid content type. image_url is only supported by certain models."),
        (422, "unknown variant `image_url`, expected `text`"),
        (500, "image input is not supported - hint: you may need to provide the mmproj"),
        (400, "This model does not support vision"),
    ],
)
def test_an_image_refusal_is_asked_once_more_without_it_and_remembered(app, no_vision, status, msg):
    endpoint = no_vision(_refusal(status, msg), "I was only told its name", "again")
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    turn = app.ask(d, "What do u see?", attachments=[png["id"]]).json()["turn"]
    assert turn["status"] == "done" and turn["answer"] == "I was only told its name"
    *refused, retry = endpoint.bodies
    assert refused and all(_images(_question(b)) for b in refused)
    retry = _question(retry)
    assert isinstance(retry, str)  # no parts — and the note no longer says "attached"
    assert ask_attachments.IMAGE_NOT_SHOWN in retry and ask_attachments.IMAGE_SHOWN not in retry
    # The endpoint said it cannot: the next image goes note-only, in ONE request.
    sent = len(endpoint.bodies)
    png2 = _publish(d, "two.png", PNG)
    turn = app.ask(d, "and this one?", attachments=[png2["id"]]).json()["turn"]
    assert turn["answer"] == "again" and len(endpoint.bodies) == sent + 1
    assert ask_attachments.IMAGE_NOT_SHOWN in _question(endpoint.bodies[-1])


def test_a_413_is_retried_without_the_image_but_not_remembered(app, no_vision):
    endpoint = no_vision(httpx.Response(413, text="Request Entity Too Large"), "ok", "ok")
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    assert app.ask(d, "look", attachments=[png["id"]]).json()["turn"]["status"] == "done"
    assert len(endpoint.bodies) == 2 and isinstance(_question(endpoint.bodies[1]), str)
    png2 = _publish(d, "two.png", PNG)
    app.ask(d, "and this?", attachments=[png2["id"]])
    assert _images(_question(endpoint.bodies[2]))  # a size problem says nothing of the endpoint


@pytest.mark.parametrize(
    "msg",
    [
        "You uploaded an unsupported image. Please make sure it is a png.",
        "image format is not supported: heic",
        "Only supported image types are png, jpeg, gif and webp",
        "image exceeds the maximum dimensions supported (8000x8000)",
        "This model does not support images in heic format",  # capability words, ONE picture
    ],
)
def test_one_bad_picture_is_retried_without_it_but_not_remembered(app, no_vision, msg):
    endpoint = no_vision(_refusal(400, msg), "ok", "ok")
    d = app.new()
    png = _publish(d, "odd.png", PNG)
    assert app.ask(d, "look", attachments=[png["id"]]).json()["turn"]["status"] == "done"
    assert isinstance(_question(endpoint.bodies[-1]), str)
    sent = len(endpoint.bodies)
    png2 = _publish(d, "fine.png", PNG)
    app.ask(d, "and this?", attachments=[png2["id"]])
    assert _images(_question(endpoint.bodies[sent]))  # vision stays on for the next picture


@pytest.mark.parametrize(
    "reply,detail",
    [
        (httpx.Response(401, text="bad key"), "HTTP 401"),
        (httpx.Response(429, text="slow down, image quota"), "HTTP 429"),
        (_refusal(400, "temperature must be between 0 and 2"), "HTTP 400"),
        (httpx.Response(500, text="boom"), "HTTP 500"),
        (httpx.Response(500, text="worker crashed while decoding image 1"), "HTTP 500"),
    ],
)
def test_unrelated_errors_are_never_retried_or_masked(app, ai, reply, detail):
    script = ai(reply)
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    turn = app.ask(d, "look", attachments=[png["id"]]).json()["turn"]
    # Never asked again WITHOUT the image (the transport's own degrade may re-send it as is).
    assert all(_images(_question(b)) for b in script.bodies)
    assert turn["status"] == "failed" and detail in turn["detail"]
    assert ask_orchestrator._IMAGES_REFUSED == set()


def test_a_vision_models_name_in_an_unrelated_error_is_not_an_image_refusal(app, ai):
    # A proxy names the model in its errors; `…-vision-…` must not turn a context-length error
    # into a silent note-only success, nor switch vision off for that model.
    prefs.set_ai_review(
        {
            "egress_acknowledged": True,
            "enabled": True,
            "base_url": BASE,
            "api_key": KEY,
            "model": "llama-3.2-11b-vision-instruct",
        }
    )
    script = ai(
        _refusal(
            400,
            "litellm.BadRequestError: Received Model Group=llama-3.2-11b-vision-instruct — "
            "maximum context length is 8192 tokens",
        )
    )
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    turn = app.ask(d, "look", attachments=[png["id"]]).json()["turn"]
    assert turn["status"] == "failed" and "HTTP 400" in turn["detail"]
    assert all(_images(_question(b)) for b in script.bodies)
    assert ask_orchestrator._IMAGES_REFUSED == set()


def test_a_validation_error_echoing_the_request_is_not_an_image_refusal(app, ai):
    # The body echoes the request's `image_url` parts as the INPUT; only its messages count.
    echoed = {
        "detail": [
            {
                "type": "missing",
                "loc": ["body", "max_tokens"],
                "msg": "Field required",
                "input": {"messages": [{"content": [{"type": "image_url"}]}]},
            }
        ]
    }
    script = ai(httpx.Response(422, json=echoed))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    turn = app.ask(d, "look", attachments=[png["id"]]).json()["turn"]
    assert turn["status"] == "failed" and "HTTP 422" in turn["detail"]
    assert all(_images(_question(b)) for b in script.bodies)


def test_a_turn_without_images_is_never_retried_on_an_image_shaped_error(app, ai):
    script = ai(_refusal(400, "image_url is only supported by certain models"))
    turn = app.ask(app.new(), "hi").json()["turn"]
    assert turn["status"] == "failed"
    assert all(_question(b) == "hi" for b in script.bodies)


# ---- not shown --------------------------------------------------------------------------------


def test_an_oversized_image_is_named_not_shown(app, ai, monkeypatch):
    monkeypatch.setattr(ask_attachments, "PROMPT_IMAGE_MAX", len(PNG) - 1)
    script = ai(answer("ok"))
    d = app.new()
    png = _publish(d, "huge.png", PNG)
    app.ask(d, "look", attachments=[png["id"]])
    content = _question(script.bodies[0])
    assert isinstance(content, str)
    assert "huge.png" in content and ask_attachments.IMAGE_NOT_SHOWN in content


def test_the_turn_budget_shows_what_fits_and_names_the_rest(app, ai, monkeypatch):
    monkeypatch.setattr(ask_attachments, "PROMPT_IMAGES_TOTAL_MAX", len(PNG) + len(JPEG) - 1)
    script = ai(answer("ok"))
    d = app.new()
    png = _publish(d, "first.png", PNG)
    jpg = _publish(d, "second.jpg", JPEG)
    app.ask(d, "look", attachments=[png["id"], jpg["id"]])
    content = _question(script.bodies[0])
    assert _images(content) == [_data_url("image/png", PNG)]
    text = _text(content)
    shown, not_shown = text.split("second.jpg")
    assert ask_attachments.IMAGE_SHOWN in shown
    assert not_shown.startswith(" (image, ") and ask_attachments.IMAGE_NOT_SHOWN in not_shown


def test_a_missing_or_mis_sniffed_image_contributes_no_bytes():
    cid = ask_store.create()
    real = _publish(cid, "shot.png", PNG)
    log = _publish(cid, "deploy.log", LOG)
    forged = {**log, "kind": "image"}  # the descriptor says image; the bytes are text
    gone = {"id": ask_attachments.new_aid(), "name": "gone.png", "kind": "image", "size": 9}
    out = ask_attachments.read_image_bodies(cid, [forged, gone, real])
    assert out == {real["id"]: ("image/png", PNG)}


def test_a_tools_refusal_falls_back_with_the_image_named_not_shown(app, ai, found):
    ai(httpx.Response(400, text='{"error": "tools are not supported by this model"}'))
    d = app.new()
    png = _publish(d, "shot.png", PNG)
    turn = app.ask(d, "What do u see?", attachments=[png["id"]]).json()["turn"]
    assert turn["status"] == "done" and turn["answer"] == "found it"
    assert len(found) == 1 and isinstance(found[0], str)
    assert "shot.png" in found[0] and ask_attachments.IMAGE_NOT_SHOWN in found[0]
    assert ask_attachments.IMAGE_SHOWN not in found[0]


def test_the_call_judgement_never_carries_the_image(ai):
    script = ai(answer("yes"))
    turn = ask_orchestrator._Turn(
        cid="ask_" + "0" * 32,
        turn_id="00000000-0000-4000-8000-000000000003",
        question="that's all, bye",
        working_keys=set(),
        progress=lambda ev: None,
        via="call",
        attachments=[{"id": "a" * 32, "name": "shot.png", "kind": "image", "size": len(PNG)}],
        image_bodies={"a" * 32: ("image/png", PNG)},
    )
    cfg = review._require_config()
    assert asyncio.run(ask_orchestrator._call_is_over(turn, cfg, "Signing off.")) is True
    said = _question(script.bodies[0])
    assert isinstance(said, str) and "shot.png" in said
    assert ask_attachments.IMAGE_NOT_SHOWN in said and "image_url" not in json.dumps(script.bodies)


def test_the_memo_follows_the_model_auto_actually_sends(monkeypatch):
    monkeypatch.setattr(ai_providers, "_listed", {})
    cfg = {
        "provider": "openai",
        "model_mode": "auto",
        "model": "gpt-4.1-mini",  # the turn's snapshot, from before the provider listed
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
    }
    fp = ai_providers.fingerprint("sk-test")
    ai_providers.remember_listing(cfg, fp, ["gpt-5-mini"])
    assert ask_orchestrator._vision_key(cfg)[1] == "gpt-5-mini"
    ai_providers.remember_listing(cfg, fp, ["gpt-5"])
    assert ask_orchestrator._vision_key(cfg)[1] == "gpt-5"  # another model, another verdict
    assert ask_orchestrator._vision_key({**cfg, "model_mode": "specific"})[1] == "gpt-4.1-mini"
