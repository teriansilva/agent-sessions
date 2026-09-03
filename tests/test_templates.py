"""The instruction-template library and its routes (#905, P1).

Three contracts this pins that nothing else does:

* **The store cannot be tricked into naming a file outside the uploads folder** — nothing is
  ever ``resolve()``d. On write an image path is checked textually (absolute, no ``..``, a
  direct child of ``uploads_dir()``, stored-shape name) and then opened ``O_NOFOLLOW`` through
  the descriptor-bound folder; on read-back ``GET /api/uploads/{stored}`` does the same open
  and streams from that descriptor — the open is the check.
* **A damaged library is quarantined, never overwritten** — a create after corruption leaves the
  prior bytes in ``templates.json.corrupt-*``, and a refused write leaves the damaged file alone.
* **Edits are fenced by ``expected_updated_at``** — a stale PATCH or DELETE is a 409 that changes
  nothing, and a ``used`` bump never moves ``updated_at`` (so it cannot invalidate an open editor).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import templates
from agent_sessions.main import create_app
from agent_sessions.routes.upload import ensure_uploads_dir

PNG = b"\x89PNG\r\n\x1a\n fake png bytes"


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


def _hdr(cfg, csrf):
    return {"X-CSRF-Token": csrf, "Origin": cfg.origin}


def _upload(c, cfg, csrf, name="shot.png", data=PNG, mime="image/png"):
    r = c.post("/api/upload", files={"file": (name, data, mime)}, headers=_hdr(cfg, csrf))
    assert r.status_code == 200, r.text
    return r.json()


def _put_upload(tmp_home: Path, stored: str, data: bytes = PNG) -> Path:
    """Drop a file straight into the (isolated) uploads folder, bypassing the route."""
    p = ensure_uploads_dir() / stored
    p.write_bytes(data)
    return p


def _payload(**over):
    base = {
        "name": "PR review checklist",
        "description": "Review a PR against the checklist.",
        "tags": ["review", "forgejo"],
        "body": "Review PR {{pr_url}} against our checklist.\n\n1. Security.\n2. Tests.",
        "fields": [
            {"name": "pr_url", "label": "PR link", "required": True},
            {"name": "issue_ref", "default": "the linked issue"},
        ],
        "images": [],
    }
    base.update(over)
    return base


def _create(c, cfg, csrf, **over):
    r = c.post("/api/templates", json=_payload(**over), headers=_hdr(cfg, csrf))
    assert r.status_code == 201, r.text
    return r.json()


# ---- round trip ------------------------------------------------------------------------------


def test_create_list_get_round_trip(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    up = _upload(c, auth_cfg, csrf)
    rec = _create(c, auth_cfg, csrf, images=[{"name": "shot.png", "path": up["path"]}])

    assert rec["id"] == "pr-review-checklist"
    assert rec["used_count"] == 0 and rec["last_used_at"] is None
    assert rec["created_at"] == rec["updated_at"]
    # Optional field keys are normalized on the way in, not left for the client to default.
    assert rec["fields"][0] == {
        "name": "pr_url",
        "label": "PR link",
        "default": "",
        "required": True,
    }
    assert rec["fields"][1] == {
        "name": "issue_ref",
        "label": "issue_ref",
        "default": "the linked issue",
        "required": False,
    }
    assert rec["images"] == [{"name": "shot.png", "path": up["path"]}]

    listed = c.get("/api/templates").json()
    assert [t["id"] for t in listed["templates"]] == ["pr-review-checklist"]
    assert listed["limits"]["templates_max"] == templates.TEMPLATES_MAX
    assert listed["limits"]["body_max"] == templates.BODY_MAX

    # On disk: owner-only, the documented shape.
    store = templates.store_path()
    assert store == tmp_home / ".config" / "agent-sessions" / "templates.json"
    assert store.stat().st_mode & 0o777 == 0o600
    doc = json.loads(store.read_text())
    assert doc["version"] == 1 and len(doc["templates"]) == 1


def test_id_is_minted_from_name_and_never_collides(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    a = _create(c, auth_cfg, csrf, name="Deploy watch!")
    b = _create(c, auth_cfg, csrf, name="deploy   WATCH")
    d = _create(c, auth_cfg, csrf, name="???")
    assert a["id"] == "deploy-watch"
    assert b["id"] == "deploy-watch-2"
    assert d["id"] == "template"


def test_crlf_is_normalized_and_the_body_is_otherwise_verbatim(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _create(c, auth_cfg, csrf, body="  line one\r\nline two\rline three  \n")
    assert rec["body"] == "  line one\nline two\nline three  \n"


def test_list_orders_by_last_use_then_edit(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    first = _create(c, auth_cfg, csrf, name="first")
    second = _create(c, auth_cfg, csrf, name="second")
    assert [t["id"] for t in c.get("/api/templates").json()["templates"]] == ["second", "first"]
    r = c.post(f"/api/templates/{first['id']}/used", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200
    assert [t["id"] for t in c.get("/api/templates").json()["templates"]] == ["first", "second"]
    assert second["last_used_at"] is None


# ---- every bound rejected, nothing persisted -------------------------------------------------


@pytest.mark.parametrize(
    "over, detail",
    [
        ({"name": ""}, "name is required"),
        ({"name": "x" * (templates.NAME_MAX + 1)}, "too long"),
        ({"name": "two\nlines"}, "single line"),
        ({"description": "d" * (templates.DESCRIPTION_MAX + 1)}, "too long"),
        ({"body": ""}, "body is required"),
        ({"body": "   \n"}, "body is required"),
        ({"body": "b" * (templates.BODY_MAX + 1)}, "too long"),
        ({"body": "run \x1b[201~ then rm -rf"}, "control character"),
        ({"body": "nul\x00byte"}, "control character"),
        ({"body": "c1\x9bcontrol"}, "control character"),
        ({"tags": ["Review"]}, "a tag is"),
        ({"tags": ["a", "a"]}, "duplicate tag"),
        ({"tags": [f"t{i}" for i in range(templates.TAGS_MAX + 1)]}, "too many tags"),
        ({"tags": "review"}, "tags must be a list"),
        ({"fields": [{"name": "PrUrl"}]}, "a field name is"),
        ({"fields": [{"name": "a"}, {"name": "a"}]}, "duplicate field"),
        ({"fields": [{"name": f"f{i}"} for i in range(templates.FIELDS_MAX + 1)]}, "too many"),
        ({"fields": [{"name": "a", "label": "l" * (templates.LABEL_MAX + 1)}]}, "too long"),
        ({"fields": [{"name": "a", "default": "d" * (templates.DEFAULT_MAX + 1)}]}, "too long"),
        ({"fields": [{"name": "a", "required": "yes"}]}, "required must be"),
        ({"fields": [{"name": "a", "probe": "http://x"}]}, "unknown field keys"),
        ({"images": [{"name": "x", "path": ""}]}, "non-empty"),
        ({"images": [{"path": "/etc/passwd"}]}, "outside the upload folder"),
        ({"images": [{"path": "../../../etc/passwd"}]}, "outside the upload folder"),
        ({"images": [{"path": "/tmp/x.png", "extra": 1}]}, "unknown image keys"),
        ({"id": "hand-picked"}, "unknown fields"),
        ({"updated_at": 1.0}, "unknown fields"),
        ({"used_count": 99}, "unknown fields"),
    ],
)
def test_every_bound_is_refused_and_nothing_is_persisted(auth_cfg, tmp_home, over, detail):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates", json=_payload(**over), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422, r.text
    assert detail in r.json()["detail"]
    assert not templates.store_path().exists()
    assert c.get("/api/templates").json()["templates"] == []


def test_image_must_be_inside_uploads_and_be_a_picture(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    # A `..` that walks out of the uploads folder and back in elsewhere.
    up = ensure_uploads_dir()
    outside = tmp_home / "secret.png"
    outside.write_bytes(PNG)
    sneaky = str(up / ".." / ".." / "secret.png")
    r = c.post(
        "/api/templates",
        json=_payload(images=[{"path": sneaky}]),
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 422 and "outside the upload folder" in r.json()["detail"]
    # A symlink INSIDE the folder that points out: the entry is opened O_NOFOLLOW, so it fails
    # the open whatever it points at — nothing is resolved.
    link = up / "20260903-000000-link.png"
    os.symlink(outside, link)
    r = c.post(
        "/api/templates", json=_payload(images=[{"path": str(link)}]), headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 422 and "outside the upload folder" in r.json()["detail"]
    # A real upload that is not a picture.
    txt = _upload(c, auth_cfg, csrf, name="notes.txt", data=b"hello", mime="text/plain")
    r = c.post(
        "/api/templates",
        json=_payload(images=[{"path": txt["path"]}]),
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 422 and "png, jpg, gif or webp" in r.json()["detail"]
    # Too many pictures.
    ok = _upload(c, auth_cfg, csrf)
    r = c.post(
        "/api/templates",
        json=_payload(images=[{"path": ok["path"]}] * (templates.IMAGES_MAX + 1)),
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 422 and "too many images" in r.json()["detail"]
    assert not templates.store_path().exists()


def test_library_cap(auth_cfg, tmp_home, monkeypatch):
    monkeypatch.setattr(templates, "TEMPLATES_MAX", 2)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    _create(c, auth_cfg, csrf, name="one")
    _create(c, auth_cfg, csrf, name="two")
    r = c.post("/api/templates", json=_payload(name="three"), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and "too many templates" in r.json()["detail"]
    assert len(c.get("/api/templates").json()["templates"]) == 2


# ---- the upload read-back route --------------------------------------------------------------


def test_upload_response_carries_the_stored_basename_and_it_reads_back(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    up = _upload(c, auth_cfg, csrf, name="My Shot!.png")
    assert up["name"] == "My_Shot_.png"
    assert up["stored"] == Path(up["path"]).name
    assert up["stored"] != up["name"]

    r = c.get(f"/api/uploads/{up['stored']}")
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["content-length"] == str(len(PNG))
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"

    # The response's `name` is the sanitized ORIGINAL name and names no file — Hermes' round-1
    # finding on #905: a client keying on it would 404.
    assert c.get(f"/api/uploads/{up['name']}").status_code == 404

    # And the round trip the issue asks for: upload → save a template that references it →
    # read the image back by the stored key the template's path carries.
    rec = _create(c, auth_cfg, csrf, images=[{"path": up["path"]}])
    stored = Path(rec["images"][0]["path"]).name
    assert c.get(f"/api/uploads/{stored}").content == PNG


@pytest.mark.parametrize(
    "stored",
    [
        "..",
        ".",
        "..png",
        "%2e%2e%2fsecret.png",
        "..%2Fsecret.png",
        "x" * 121 + ".png",
        "a b.png",
        "manual.png",  # a hand-placed simple basename is not something the upload route wrote
    ],
)
def test_read_back_refuses_anything_but_one_stored_component(auth_cfg, tmp_home, stored):
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    (tmp_home / "secret.png").write_bytes(PNG)
    _put_upload(tmp_home, "x" * 121 + ".png")
    _put_upload(tmp_home, "manual.png")
    r = c.get(f"/api/uploads/{stored}")
    assert r.status_code == 404, stored


def test_read_back_serves_images_only(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    txt = _upload(c, auth_cfg, csrf, name="token.txt", data=b"secret=1", mime="text/plain")
    assert c.get(f"/api/uploads/{txt['stored']}").status_code == 404
    # The suffix decides, not the bytes: a text file renamed .png is served as image/png with
    # nosniff, which is exactly why the content type is never sniffed from the payload.
    _put_upload(tmp_home, "20260903-000000-renamed.png", b"not really a png")
    r = c.get("/api/uploads/20260903-000000-renamed.png")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/png")


def test_read_back_never_follows_a_symlink_at_the_entry(auth_cfg, tmp_home):
    """The open IS the containment check (Hermes on #906): the entry is opened with
    ``O_NOFOLLOW`` relative to the uploads directory and the bytes stream from that descriptor,
    so there is no resolve-then-reopen window for a same-UID swap to race. The observable rule
    is stricter than "resolves inside": a symlink at the entry is refused even when it points
    at a perfectly good upload, because following it is exactly the step that could be raced."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    up = ensure_uploads_dir()
    outside = tmp_home / "secret.png"
    outside.write_bytes(b"outside")
    os.symlink(outside, up / "20260903-000000-link.png")
    assert c.get("/api/uploads/20260903-000000-link.png").status_code == 404
    inside = _put_upload(tmp_home, "20260903-000000-real.png")
    os.symlink(inside, up / "20260903-000000-inner.png")
    assert c.get("/api/uploads/20260903-000000-inner.png").status_code == 404
    assert c.get("/api/uploads/20260903-000000-real.png").content == PNG
    # A directory named like an image is not a regular file.
    (up / "20260903-000000-dir.png").mkdir()
    assert c.get("/api/uploads/20260903-000000-dir.png").status_code == 404


def test_every_response_on_both_surfaces_is_no_store(auth_cfg, tmp_home):
    """Uploads can be screenshots with tokens in them; templates are verbatim instructions.
    Neither may survive in a browser cache after sign-out, so success AND error responses —
    including the 401 that fires before any handler runs — carry the files-route policy."""
    _put_upload(tmp_home, "20260903-000000-shot.png")

    def no_store(r):
        assert r.headers.get("cache-control") == "no-store", (r.request.url, r.status_code)
        assert r.headers.get("pragma") == "no-cache", r.request.url

    c = _client(auth_cfg)
    no_store(c.get("/api/uploads/20260903-000000-shot.png"))  # 401
    no_store(c.get("/api/templates"))  # 401
    csrf = _login(c, auth_cfg)
    no_store(c.get("/api/uploads/20260903-000000-shot.png"))  # 200
    no_store(c.get("/api/uploads/nope.png"))  # 404
    no_store(c.get("/api/uploads/token.txt"))  # 404, non-image
    r = c.get("/api/templates")
    assert r.status_code == 200
    no_store(r)
    r = c.post("/api/templates", json=_payload(), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 201
    no_store(r)
    rec = r.json()
    r = c.post("/api/templates", json=_payload(name=""), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422
    no_store(r)
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(), "expected_updated_at": 1.0},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 409
    no_store(r)
    no_store(c.post("/api/templates/nope/used", headers=_hdr(auth_cfg, csrf)))  # 404
    no_store(c.post(f"/api/templates/{rec['id']}/used", headers=_hdr(auth_cfg, csrf)))  # 200
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at={rec['updated_at']!r}",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 204
    no_store(r)
    no_store(c.post("/api/templates", json=_payload(), headers={"Origin": auth_cfg.origin}))  # 403
    # The upload POST names an absolute host path in its response: same policy.
    r = c.post(
        "/api/upload", files={"file": ("x.png", PNG, "image/png")}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 200
    no_store(r)
    r = c.post(
        "/api/upload", files={"file": ("e.png", b"", "image/png")}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 422
    no_store(r)


def test_read_back_needs_a_session(auth_cfg, tmp_home):
    _put_upload(tmp_home, "20260903-000000-shot.png")
    c = _client(auth_cfg)
    assert c.get("/api/uploads/20260903-000000-shot.png").status_code == 401


# ---- optimistic concurrency ------------------------------------------------------------------


def test_stale_patch_is_409_and_changes_nothing(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _create(c, auth_cfg, csrf)
    # Editor A saves.
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(name="Renamed by A"), "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200
    a = r.json()
    assert a["name"] == "Renamed by A" and a["updated_at"] > rec["updated_at"]
    # Editor B, still holding the original record, saves over it.
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(name="Renamed by B"), "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 409
    assert r.json()["current"]["name"] == "Renamed by A"
    assert r.json()["current"]["updated_at"] == a["updated_at"]
    assert c.get("/api/templates").json()["templates"][0]["name"] == "Renamed by A"
    # B reloads and saves against the current revision: accepted.
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(name="Renamed by B"), "expected_updated_at": a["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200 and r.json()["name"] == "Renamed by B"


def test_stale_delete_is_409_and_the_fence_is_required(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _create(c, auth_cfg, csrf)
    r = c.delete(f"/api/templates/{rec['id']}", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and "expected_updated_at is required" in r.json()["detail"]
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at=1.5", headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 409 and r.json()["current"]["id"] == rec["id"]
    assert len(c.get("/api/templates").json()["templates"]) == 1
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at={rec['updated_at']!r}",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 204
    assert c.get("/api/templates").json()["templates"] == []
    # PATCH without the fence is refused before anything is validated or written.
    r = c.patch(f"/api/templates/{rec['id']}", json=_payload(), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422 and "expected_updated_at is required" in r.json()["detail"]


def test_used_bumps_counters_but_never_updated_at(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _create(c, auth_cfg, csrf)
    r = c.post(f"/api/templates/{rec['id']}/used", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200
    used = r.json()
    assert used["used_count"] == 1 and used["last_used_at"] is not None
    assert used["updated_at"] == rec["updated_at"]
    # An editor opened before the send can still save.
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(), "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200 and r.json()["used_count"] == 1


def test_unknown_template_is_404(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    h = _hdr(auth_cfg, csrf)
    assert c.post("/api/templates/nope/used", headers=h).status_code == 404
    assert c.delete("/api/templates/nope?expected_updated_at=1", headers=h).status_code == 404
    r = c.patch("/api/templates/nope", json={**_payload(), "expected_updated_at": 1}, headers=h)
    assert r.status_code == 404


# ---- corruption ------------------------------------------------------------------------------


def test_corrupt_store_reads_empty_and_is_quarantined_not_overwritten(
    auth_cfg, tmp_home, monkeypatch
):
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    garbage = b'{"version": 1, "templates": [ {"id": "half-written", "name": '
    store.write_bytes(garbage)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/templates").json()["templates"] == []
    # A refused write leaves the damaged file exactly where it was.
    r = c.post("/api/templates", json=_payload(name=""), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 422
    assert store.read_bytes() == garbage
    assert not list(store.parent.glob("templates.json.corrupt-*"))
    # A publish that FAILS after the quarantine step must leave the original where it was —
    # the first cut renamed it aside first, so a failed publish left no templates.json at all
    # (Hermes on #906). Now the damaged bytes are hard-linked aside and the publish is an
    # atomic replace of a name that still exists.
    real_write = templates._write

    def boom(path, records):
        raise OSError("ENOSPC")

    monkeypatch.setattr(templates, "_write", boom)
    with pytest.raises(OSError):
        templates.create_template(_payload())
    assert store.read_bytes() == garbage  # still there, still the same bytes
    aside = list(store.parent.glob("templates.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == garbage
    assert os.stat(aside[0]).st_ino == os.stat(store).st_ino  # a link, not a move
    monkeypatch.setattr(templates, "_write", real_write)
    # An accepted write publishes over the original name; the aside copy keeps the old bytes.
    _create(c, auth_cfg, csrf)
    quarantined = sorted(store.parent.glob("templates.json.corrupt-*"))
    assert len(quarantined) == 2  # one per accepted-write attempt after a damaged read
    assert all(q.read_bytes() == garbage for q in quarantined)
    doc = json.loads(store.read_text())
    assert [t["id"] for t in doc["templates"]] == ["pr-review-checklist"]


def test_unreadable_record_is_skipped_on_read_and_preserved_by_the_quarantine(auth_cfg, tmp_home):
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    good = templates.create_template(_payload(name="good"))
    doc = json.loads(store.read_text())
    doc["templates"].append({"id": "bad", "name": "no body", "images": [{"path": "/etc/passwd"}]})
    doc["templates"].append("not even an object")
    raw = json.dumps(doc).encode()
    store.write_bytes(raw)
    assert [t["id"] for t in templates.list_templates()] == [good["id"]]
    templates.create_template(_payload(name="another"))
    quarantined = list(store.parent.glob("templates.json.corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == raw
    assert sorted(t["id"] for t in templates.list_templates()) == ["another", "good"]


def test_wrong_shape_is_damaged_too(auth_cfg, tmp_home):
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    store.write_text('["a list", "not a store"]')
    assert templates.list_templates() == []
    templates.create_template(_payload())
    assert len(list(store.parent.glob("templates.json.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"updated_at": float("nan")},
        {"updated_at": float("inf")},
        {"created_at": float("-inf")},
        {"last_used_at": 10**400},
        {"created_at": -5},
        {"used_count": 10**400},
        {"updated_at": 2**60},
    ],
)
def test_non_finite_or_oversized_numbers_are_skipped_on_read_not_served(auth_cfg, tmp_home, bad):
    """``json.loads`` happily yields NaN / inf / 400-digit ints from a damaged file; the first
    cut let them through ``_coerce_record`` and ``GET /api/templates`` died in ``JSONResponse``
    (Hermes on #906). They are refused like any other unreadable record — skipped on read,
    kept aside on the next write — so the lenient-read contract holds."""
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    good = templates.create_template(_payload(name="good"))
    doc = json.loads(store.read_text())
    doc["templates"].append({**doc["templates"][0], "id": "bad", "name": "bad", **bad})
    # json.dumps writes NaN/Infinity by default, exactly what a damaged store can carry.
    store.write_text(json.dumps(doc))
    assert [t["id"] for t in templates.list_templates()] == [good["id"]]
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = c.get("/api/templates")
    assert r.status_code == 200 and [t["id"] for t in r.json()["templates"]] == [good["id"]]


def test_a_newer_store_version_is_refused_and_never_rewritten(auth_cfg, tmp_home):
    """An older binary must not coerce records it does not understand, drop the fields it
    cannot see and publish the result as version 1 (Hermes on #906). Reads serve an empty
    library; every write is a 409 that names the versions; the bytes are untouched."""
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "version": 2,
            "templates": [
                {
                    **_payload(),
                    "id": "future",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                    "used_count": 0,
                    "last_used_at": None,
                    "future_only": "keep me",
                }
            ],
        }
    ).encode()
    store.write_bytes(raw)
    assert templates.list_templates() == []
    with pytest.raises(templates.TemplateStoreUnsupported):
        templates.mark_used("future")
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/templates").json()["templates"] == []
    for r in (
        c.post("/api/templates", json=_payload(), headers=_hdr(auth_cfg, csrf)),
        c.post("/api/templates/future/used", headers=_hdr(auth_cfg, csrf)),
        c.patch(
            "/api/templates/future",
            json={**_payload(), "expected_updated_at": 1.0},
            headers=_hdr(auth_cfg, csrf),
        ),
        c.delete("/api/templates/future?expected_updated_at=1.0", headers=_hdr(auth_cfg, csrf)),
    ):
        assert r.status_code == 409, r.text
        assert "version 2" in r.json()["detail"] and "nothing was written" in r.json()["detail"]
    assert store.read_bytes() == raw
    assert not list(store.parent.glob("templates.json.corrupt-*"))


@pytest.mark.parametrize("version", [0, "1", None, 1.5, True])
def test_a_missing_or_invalid_version_is_damage_not_a_newer_store(auth_cfg, tmp_home, version):
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    doc = {"templates": []}
    if version is not None:
        doc["version"] = version
    store.write_text(json.dumps(doc))
    assert templates.list_templates() == []
    templates.create_template(_payload())
    assert len(list(store.parent.glob("templates.json.corrupt-*"))) == 1
    assert json.loads(store.read_text())["version"] == templates.STORE_VERSION


def test_an_image_reference_must_be_what_the_read_back_route_can_serve(auth_cfg, tmp_home):
    """What ``validate`` accepts and what ``GET /api/uploads/{stored}`` serves are one contract
    (Hermes on #906): an existing regular file DIRECTLY inside the folder, in the stored shape.
    Anything else would save fine and then show a thumbnail that can never load."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    up = ensure_uploads_dir()
    cases = {
        str(up / "20260903-000000-missing.png"): "image does not exist",
        str(up / "sub" / "20260903-000000-nested.png"): "directly inside the upload folder",
        str(up / "20260903-000000-dir.png"): "image does not exist",
        str(up / "not an upload.png"): "a file the upload route wrote",
    }
    (up / "sub").mkdir()
    (up / "sub" / "20260903-000000-nested.png").write_bytes(PNG)
    (up / "20260903-000000-dir.png").mkdir()
    (up / "not an upload.png").write_bytes(PNG)
    for path, detail in cases.items():
        r = c.post(
            "/api/templates", json=_payload(images=[{"path": path}]), headers=_hdr(auth_cfg, csrf)
        )
        assert r.status_code == 422, path
        assert detail in r.json()["detail"], (path, r.json())
    assert not templates.store_path().exists()
    # And the positive case: a real upload is accepted and reads back by its stored name.
    real = _upload(c, auth_cfg, csrf)
    rec = _create(c, auth_cfg, csrf, images=[{"path": real["path"]}])
    assert c.get(f"/api/uploads/{Path(rec['images'][0]['path']).name}").content == PNG


def test_a_failed_upload_is_discarded_through_the_bound_folder_never_by_pathname(
    auth_cfg, tmp_home, monkeypatch
):
    """The cleanup of a failed upload used to be `dest.unlink()` — a pathname lookup after the
    folder descriptor was closed. Swap the folder for a symlink during the failure and that
    lookup deleted the same basename in the symlink's target while the partial file stayed in
    the real folder (Hermes on #906, round 3). Now the entry is unlinked through the descriptor
    of the folder that received it, and the sentinel elsewhere is untouched."""
    import errno
    import time as _time

    monkeypatch.setattr(_time, "strftime", lambda fmt: "20260903-000000")
    real = ensure_uploads_dir()
    elsewhere = tmp_home / "elsewhere"
    elsewhere.mkdir()
    sentinel = elsewhere / "20260903-000000-x.png"
    sentinel.write_bytes(b"OUTSIDE-SENTINEL")
    moved = tmp_home / ".agent-sessions" / "uploads.moved"
    real_fchmod = os.fchmod

    def swap_then_fail(fd, mode):
        # The attacker's move, timed inside the failure window: rename the real folder away and
        # leave a symlink to `elsewhere` at its old name.
        os.rename(real, moved)
        os.symlink(elsewhere, real)
        raise OSError(errno.EIO, "forced")

    monkeypatch.setattr(os, "fchmod", swap_then_fail)
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    try:
        r = c.post(
            "/api/upload", files={"file": ("x.png", PNG, "image/png")}, headers=_hdr(auth_cfg, csrf)
        )
    except OSError:
        r = None  # TestClient re-raises server exceptions; the outcome on disk is what matters
    monkeypatch.setattr(os, "fchmod", real_fchmod)
    assert r is None or r.status_code == 500
    assert sentinel.read_bytes() == b"OUTSIDE-SENTINEL"  # never deleted through the symlink
    assert not (moved / "20260903-000000-x.png").exists()  # the partial file was discarded


def test_a_symlinked_uploads_folder_is_refused_everywhere(auth_cfg, tmp_home):
    """The folder is bound by descriptor with O_NOFOLLOW at every component below $HOME
    (Hermes on #906, round 2: the first fix only refused a symlink at the ENTRY, while the
    folder itself could still be swapped for a symlink and redirect the read-back). A
    symlinked uploads folder is not supported: read-back 404s, the upload POST refuses, and a
    template cannot reference anything through it."""
    elsewhere = tmp_home / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "20260903-000000-private.png").write_bytes(b"OUTSIDE-SECRET")
    (tmp_home / ".agent-sessions").mkdir()
    os.symlink(elsewhere, tmp_home / ".agent-sessions" / "uploads")
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    assert c.get("/api/uploads/20260903-000000-private.png").status_code == 404
    r = c.post(
        "/api/upload", files={"file": ("x.png", PNG, "image/png")}, headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 500 and "symlink" in r.json()["detail"]
    assert not list(elsewhere.glob("*-x.png"))  # nothing was written through the link
    through = str(tmp_home / ".agent-sessions" / "uploads" / "20260903-000000-private.png")
    r = c.post(
        "/api/templates", json=_payload(images=[{"path": through}]), headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 422 and "outside the upload folder" in r.json()["detail"]


def test_a_future_store_is_refused_before_its_shape_is_judged(auth_cfg, tmp_home):
    """A version-2 store need not carry a v1 ``templates`` list; judging shape first called it
    damaged and the next write quarantined + downgraded it (Hermes on #906, round 2)."""
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    raw = json.dumps({"version": 2, "entries": [{"id": "future", "payload": "keep me"}]}).encode()
    store.write_bytes(raw)
    assert templates.list_templates() == []
    with pytest.raises(templates.TemplateStoreUnsupported):
        templates.create_template(_payload())
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates", json=_payload(), headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 409 and "version 2" in r.json()["detail"]
    assert store.read_bytes() == raw
    assert not list(store.parent.glob("templates.json.corrupt-*"))


def test_a_deleted_image_does_not_erase_the_template(auth_cfg, tmp_home):
    """Read-time coercion must not depend on mutable file existence: an upload deleted later
    leaves a template with a body, whose thumbnail simply 404s. The first cut reclassified the
    record as corrupt and the next unrelated write rewrote the store without it (Hermes on
    #906, round 2)."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    up = _upload(c, auth_cfg, csrf)
    rec = _create(c, auth_cfg, csrf, name="with image", images=[{"path": up["path"]}])
    Path(up["path"]).unlink()
    listed = c.get("/api/templates").json()["templates"]
    assert [t["id"] for t in listed] == [rec["id"]]
    assert listed[0]["images"] == [{"name": up["stored"], "path": up["path"]}]
    assert c.get(f"/api/uploads/{up['stored']}").status_code == 404
    _create(c, auth_cfg, csrf, name="unrelated")
    doc = json.loads(templates.store_path().read_text())
    assert sorted(t["id"] for t in doc["templates"]) == ["unrelated", "with-image"]
    assert not list(templates.store_path().parent.glob("templates.json.corrupt-*"))
    # The record can still be edited (the write-time check applies to NEW references only if
    # they are sent; the existing reference is re-sent as-is and re-validated textually).
    r = c.patch(
        f"/api/templates/{rec['id']}",
        json={**_payload(name="renamed", images=[]), "expected_updated_at": rec["updated_at"]},
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 200


def test_the_numeric_maximum_is_closed_under_every_mutation(auth_cfg, tmp_home):
    """``used_count == 2**53`` is accepted on read, so ``/used`` must not write 2**53 + 1 (the
    next read would hide the template); ``updated_at == 2**53`` plus ``+ 1e-3`` rounded back
    to the same float, so two edits with the same stale fence both went through (Hermes on
    #906, round 2). The counter saturates; the revision refuses without writing."""
    top = float(2**53)
    store = templates.store_path()
    store.parent.mkdir(parents=True)
    rec = templates.create_template(_payload(name="edge"))
    doc = json.loads(store.read_text())
    doc["templates"][0]["used_count"] = 2**53
    doc["templates"][0]["updated_at"] = top
    store.write_text(json.dumps(doc))
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    r = c.post(f"/api/templates/{rec['id']}/used", headers=_hdr(auth_cfg, csrf))
    assert r.status_code == 200 and r.json()["used_count"] == 2**53
    assert [t["id"] for t in c.get("/api/templates").json()["templates"]] == [rec["id"]]
    for _ in range(2):
        r = c.patch(
            f"/api/templates/{rec['id']}",
            json={**_payload(name="edge 2"), "expected_updated_at": top},
            headers=_hdr(auth_cfg, csrf),
        )
        assert r.status_code == 422 and "exhausted" in r.json()["detail"]
    assert c.get("/api/templates").json()["templates"][0]["name"] == "edge"
    assert json.loads(store.read_text())["templates"][0]["updated_at"] == top
    # One past the maximum is not "equal after rounding": a fence of 2**53 + 1 against a record
    # at 2**53 is a 422, not a match (Hermes on #906, round 3).
    r = c.patch(
        f"/api/templates/{rec['id']}",
        content=json.dumps({**_payload(name="edge 2"), "expected_updated_at": 2**53 + 1}),
        headers={**_hdr(auth_cfg, csrf), "Content-Type": "application/json"},
    )
    assert r.status_code == 422 and "out of range" in r.json()["detail"]
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at=9007199254740993",
        headers=_hdr(auth_cfg, csrf),
    )
    assert r.status_code == 422 and "out of range" in r.json()["detail"]
    assert len(c.get("/api/templates").json()["templates"]) == 1
    # Delete needs no bump and still works at the boundary with the exact fence.
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at={top!r}", headers=_hdr(auth_cfg, csrf)
    )
    assert r.status_code == 204


@pytest.mark.parametrize(
    "fence, where",
    [
        (10**400, "body"),
        (2**53 + 1, "body"),  # one past the max: must not round down onto 2**53 via float()
        ("9007199254740993", "query"),  # the same, as the DELETE query string
        ("9007199254740993.0", "query"),
        (float("inf"), "query"),
        ("1e400", "query"),
        ("nan", "query"),
        ("abc", "query"),
        (-1, "body"),
        (True, "body"),
    ],
)
def test_the_fence_parser_validates_like_a_stored_timestamp(auth_cfg, tmp_home, fence, where):
    """A 400-digit int used to escape ``float()`` as a 500; the fence now passes the same
    finite / non-negative / <= 2**53 rule as every stored timestamp, so it is a 422."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    rec = _create(c, auth_cfg, csrf)
    if where == "body":
        body = json.dumps({**_payload(), "expected_updated_at": fence})
        r = c.patch(
            f"/api/templates/{rec['id']}",
            content=body,
            headers={**_hdr(auth_cfg, csrf), "Content-Type": "application/json"},
        )
    else:
        r = c.delete(
            f"/api/templates/{rec['id']}?expected_updated_at={fence}", headers=_hdr(auth_cfg, csrf)
        )
    assert r.status_code == 422, r.text
    assert "expected_updated_at" in r.json()["detail"]
    assert len(c.get("/api/templates").json()["templates"]) == 1


# ---- concurrency + auth ----------------------------------------------------------------------


def test_concurrent_creates_serialize_under_the_lock(tmp_home):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(4):
                templates.create_template(_payload(name=f"worker {n} template {i}"))
        except BaseException as e:  # noqa: BLE001 — surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = [t["id"] for t in templates.list_templates()]
    assert len(ids) == 24 and len(set(ids)) == 24


def test_reads_need_a_session_and_writes_need_csrf(auth_cfg, tmp_home):
    c = _client(auth_cfg)
    assert c.get("/api/templates").status_code == 401
    csrf = _login(c, auth_cfg)
    r = c.post("/api/templates", json=_payload(), headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403
    rec = _create(c, auth_cfg, csrf)
    r = c.post(f"/api/templates/{rec['id']}/used", headers={"Origin": auth_cfg.origin})
    assert r.status_code == 403
    r = c.delete(
        f"/api/templates/{rec['id']}?expected_updated_at={rec['updated_at']!r}",
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 403
    assert len(c.get("/api/templates").json()["templates"]) == 1
