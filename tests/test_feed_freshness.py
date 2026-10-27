"""Freshness statements for the two signed public feeds (#1730): release-approved long cuts are
usable only while a current, low-authority statement names exactly them; short cuts keep the
legacy path; the statement floor refuses rollback and equivocation and reuses the identical
statement idempotently. Real ssh-keygen signatures, both admission paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess

import httpx
import pytest

import test_plugin_feed as tpf
from agent_sessions.plugins import feed, freshness, freshness_client, storage

NOW = tpf.NOW
DAY = 86400


def _key(tmp_path, name):
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


def _sign(key, data, namespace):
    return subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A release key (content) and a separate freshness key, each pinned in its own file."""
    release, fresh_key = _key(tmp_path, "release"), _key(tmp_path, "freshness")
    allowed = tmp_path / "release-signers"
    allowed.write_text(feed.PRINCIPAL + " " + release.with_suffix(".pub").read_text())
    monkeypatch.setattr(feed, "SIGNERS", allowed)
    allowed_f = tmp_path / "freshness-signers"
    allowed_f.write_text(freshness.PRINCIPAL + " " + fresh_key.with_suffix(".pub").read_text())
    monkeypatch.setattr(freshness, "SIGNERS", allowed_f)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))

    class K:
        def content(self, data, namespace=feed.NAMESPACE):
            return _sign(release, data, namespace)

        def statement(self, data, key=None):
            return _sign(key or fresh_key, data, freshness.NAMESPACE)

    K.tmp = tmp_path
    return K()


def cut(sequence=1, *, long=True):
    doc = tpf.document(sequence)
    doc["issued_at"] = NOW - 60
    doc["expires_at"] = NOW + (60 * DAY if long else DAY)
    data = feed.canonical(doc)
    return data, doc


def stmt(
    content_data,
    content_doc,
    *,
    sequence=1,
    feed_name="agents",
    issued=None,
    expires=None,
    content_sequence=None,
    content_digest=None,
):
    body = {
        "format": 1,
        "feed": feed_name,
        "statement_sequence": sequence,
        "content_sequence": (
            content_doc["sequence"] if content_sequence is None else content_sequence
        ),
        "content_digest": content_digest or hashlib.sha256(content_data).hexdigest(),
        "issued_at": NOW - 30 if issued is None else issued,
        "expires_at": NOW + 2 * DAY if expires is None else expires,
    }
    return feed.canonical(body)


def ag_floor():
    return storage.root() / f"{feed.STATEMENT}-floor.json"


def ag_record(content):
    """The agent feed's statement record for one content cut (bytes)."""
    return storage.root() / f"{feed.STATEMENT}-{hashlib.sha256(content).hexdigest()}.json"


def bundle(keys, data, *, signature=None, pointer=None):
    signature = keys.statement(data) if signature is None else signature
    if pointer is None:
        pointer = {
            "sequence": json.loads(data)["statement_sequence"],
            "digest": hashlib.sha256(data).hexdigest(),
        }
    return data, signature, pointer


# ---- the statement ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"format": 2},
        {"feed": "plugins"},
        {"statement_sequence": 0},
        {"content_digest": "x" * 64},
        {"issued_at": NOW + 400},
        {"expires_at": NOW - 1},
        {"expires_at": NOW - 30 + freshness.MAX_STATEMENT_AGE + 1},
        {"extra": 1},
    ],
)
def test_a_malformed_or_out_of_window_statement_is_refused(change):
    data, doc = cut()
    body = json.loads(stmt(data, doc))
    body.update(change)
    with pytest.raises(freshness.FreshnessError):
        freshness.parse(feed.canonical(body), now=NOW)


def test_a_statement_signed_by_the_release_key_or_another_key_is_refused(keys):
    data, doc = cut()
    s = stmt(data, doc)
    for signature in (keys.content(s, freshness.NAMESPACE), keys.statement(s, _key(keys.tmp, "x"))):
        with pytest.raises(freshness.FreshnessError, match="trusted freshness signer"):
            freshness.checked(s, signature, feed_name="agents", now=NOW)


# ---- the agent catalog --------------------------------------------------------------------------


def test_a_short_cut_needs_no_statement(keys):
    data, _ = cut(long=False)
    snapshot = feed.accept(data, keys.content(data), now=NOW)
    assert feed.fresh(snapshot, now=NOW)


def test_a_long_cut_is_refused_without_a_statement_and_nothing_is_stored(keys):
    data, _ = cut()
    with pytest.raises(feed.FeedError, match="no current freshness statement"):
        feed.accept(data, keys.content(data), now=NOW)
    assert not (storage.root() / "feed.json").exists()


def test_a_long_cut_with_its_statement_is_accepted_and_fresh_until_the_statement_expires(keys):
    data, doc = cut()
    snapshot = feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW
    )
    assert feed.fresh(snapshot, now=NOW)
    assert feed.current(now=NOW).digest == snapshot.digest
    later = NOW + 3 * DAY  # the statement expired; the content's own window has not
    assert not feed.fresh(snapshot, now=later)
    with pytest.raises(feed.FeedError):
        feed.current(now=later)


def test_a_daily_renewal_of_unchanged_content_keeps_it_fresh_and_advances_the_floor(keys):
    data, doc = cut()
    feed.accept(data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW)
    tomorrow = NOW + DAY
    renewed = stmt(data, doc, sequence=2, issued=tomorrow - 30, expires=tomorrow + 2 * DAY)
    snapshot = feed.accept(data, keys.content(data), statement=bundle(keys, renewed), now=tomorrow)
    assert feed.fresh(snapshot, now=NOW + 2 * DAY + DAY // 2)  # beyond the first statement
    floor = storage.read(ag_floor())
    assert floor["sequence"] == 2


def test_the_identical_statement_again_is_an_idempotent_reuse(keys):
    data, doc = cut()
    b = bundle(keys, stmt(data, doc))
    feed.accept(data, keys.content(data), statement=b, now=NOW)
    feed.accept(data, keys.content(data), statement=b, now=NOW + 60)  # retry / restart
    assert storage.read(ag_floor())["sequence"] == 1


def test_rollback_and_equivocation_are_refused_and_leave_evidence_intact(keys):
    data, doc = cut()
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=5)), now=NOW
    )
    before = storage.read(ag_record(data))
    older = stmt(data, doc, sequence=4)
    same_seq_other_bytes = stmt(data, doc, sequence=5, expires=NOW + DAY)
    for s in (older, same_seq_other_bytes):
        with pytest.raises(freshness.FreshnessError, match="backwards|different statement"):
            feed.accept(data, keys.content(data), statement=bundle(keys, s), now=NOW)
    assert storage.read(ag_record(data)) == before


@pytest.mark.parametrize(
    "bad",
    [
        {"content_digest": "b" * 64},
        {"content_sequence": 9},
        {"expires": NOW + 61 * DAY},  # outlives the 60-day content
        {"feed_name": "gallery"},
    ],
)
def test_a_statement_that_does_not_name_exactly_this_cut_refuses_a_long_cut(keys, bad):
    data, doc = cut()
    s = stmt(data, doc, **bad)
    with pytest.raises(feed.FeedError, match="not current"):
        feed.accept(data, keys.content(data), statement=bundle(keys, s), now=NOW)
    assert not (storage.root() / "feed.json").exists()


def test_a_pointer_that_differs_from_the_signed_statement_is_refused(keys):
    data, doc = cut()
    s = stmt(data, doc, sequence=3)
    for pointer in (
        {"sequence": 4, "digest": hashlib.sha256(s).hexdigest()},
        {"sequence": 3, "digest": "c" * 64},
    ):
        with pytest.raises(feed.FeedError, match="not current"):
            feed.accept(
                data, keys.content(data), statement=bundle(keys, s, pointer=pointer), now=NOW
            )


def test_the_pointer_moved_before_the_cut_long_fails_closed_short_ignores_it(keys):
    """Review 110085: the statement already names a newer cut than the one fetched."""
    old, old_doc = cut(1)
    feed.accept(old, keys.content(old), statement=bundle(keys, stmt(old, old_doc)), now=NOW)
    accepted = storage.read(storage.root() / "feed.json")
    newer, newer_doc = cut(2)
    ahead = bundle(keys, stmt(newer, newer_doc, sequence=2))
    with pytest.raises(feed.FeedError, match="not current"):
        feed.accept(old, keys.content(old), statement=ahead, now=NOW)  # cached old cut
    assert storage.read(storage.root() / "feed.json") == accepted
    snapshot = feed.accept(newer, keys.content(newer), statement=ahead, now=NOW)  # recovers
    assert feed.fresh(snapshot, now=NOW)
    short, _ = cut(3, long=False)
    snapshot = feed.accept(short, keys.content(short), statement=ahead, now=NOW)
    assert feed.fresh(snapshot, now=NOW)  # a short cut needs none, and ignores the stale one


@pytest.mark.parametrize("boundary", ["floor", "record"])
def test_an_interrupted_first_admission_accepts_nothing_until_a_retry(keys, monkeypatch, boundary):
    """The first admission writes the statement floor, then its record, then the content: an
    interruption at either freshness write accepts no content, and a retry completes."""
    data, doc = cut()
    b = bundle(keys, stmt(data, doc))
    target = (ag_floor() if boundary == "floor" else ag_record(data)).name
    real = storage.write

    def crash(path, value, **kw):
        if path.name == target:
            raise OSError("disk full")
        return real(path, value, **kw)

    monkeypatch.setattr(storage, "write", crash)
    with pytest.raises(OSError):
        feed.accept(data, keys.content(data), statement=b, now=NOW)
    monkeypatch.setattr(storage, "write", real)
    assert feed.historical() is None
    snapshot = feed.accept(data, keys.content(data), statement=b, now=NOW)  # the retry completes
    assert feed.fresh(snapshot, now=NOW) and storage.read(ag_floor())["sequence"] == 1


def test_a_tampered_stored_statement_is_not_fresh_after_a_restart(keys):
    data, doc = cut()
    snapshot = feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW
    )
    path = ag_record(data)
    record = storage.read(path)
    record["signature"] = keys.statement(stmt(data, doc, expires=NOW + DAY)).decode("ascii")
    storage.write(path, record)
    assert not feed.fresh(snapshot, now=NOW)


def test_the_agent_catalog_reads_a_long_cut_as_stale_once_its_statement_expires(keys):
    from agent_sessions import agent_catalog

    data, doc = cut()
    feed.accept(data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW)
    import time as _time

    real = _time.time
    try:
        _time.time = lambda: NOW
        assert agent_catalog.current().stale is False
        _time.time = lambda: NOW + 3 * DAY
        assert agent_catalog.current().stale is True
    finally:
        _time.time = real


# ---- fetching ------------------------------------------------------------------------------------


def test_fetch_follows_the_pointer_to_an_immutable_generation_and_reports_none_on_failure():
    data = b'{"x":1}'
    digest = hashlib.sha256(data).hexdigest()
    pointer = feed.canonical({"digest": digest, "sequence": 7})
    base = freshness_client.BASE + "/agents"
    routes = {
        base + "/current.json": pointer,
        f"{base}/releases/7-{digest}/statement.json": data,
        f"{base}/releases/7-{digest}/statement.json.sig": b"sig",
    }

    def handler(request):
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, stream=httpx.ByteStream(body))

    async def go(transport):
        async with httpx.AsyncClient(transport=transport) as client:
            return await freshness_client.fetch(client, "agents")

    got = asyncio.run(go(httpx.MockTransport(handler)))
    assert got == (data, b"sig", {"digest": digest, "sequence": 7})
    assert asyncio.run(go(httpx.MockTransport(lambda r: httpx.Response(404)))) is None
    routes[base + "/current.json"] = b'{"sequence":"7"}'
    assert asyncio.run(go(httpx.MockTransport(handler))) is None


# ---- the gallery --------------------------------------------------------------------------------

FIXTURES = __import__("pathlib").Path(__file__).with_name("fixtures") / "gallery"


@pytest.fixture
def gallery(keys, tmp_path, monkeypatch):
    from agent_sessions.gallery import contract, trust

    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(tmp_path / "playbooks"))
    clock = {"now": NOW}
    monkeypatch.setattr(contract.time, "time", lambda: clock["now"])
    monkeypatch.setattr(freshness.time, "time", lambda: clock["now"])

    def index(sequence=1, *, long=True):
        doc = json.loads((FIXTURES / "index.json").read_bytes())
        doc.update(sequence=sequence, issued_at=NOW - 60)
        doc["expires_at"] = NOW + (60 * DAY if long else DAY)
        raw = contract.canonical(doc)
        return raw, doc, keys.content(raw, contract.NAMESPACE)

    def accept(raw, doc, signature, statement=None):
        selected = {"sequence": doc["sequence"], "digest": hashlib.sha256(raw).hexdigest()}
        return trust.accept(raw, signature, selected, statement)

    return index, accept, clock, trust, contract


def test_gallery_long_cut_needs_its_statement_and_is_fresh_while_it_lasts(keys, gallery):
    index, accept, clock, trust, contract = gallery
    raw, doc, sig = index()
    with pytest.raises(contract.GalleryError, match="no current freshness statement"):
        accept(raw, doc, sig)
    record = accept(raw, doc, sig, bundle(keys, stmt(raw, doc, feed_name="gallery")))
    trust.checked(record)  # usable now
    clock["now"] = NOW + 3 * DAY  # statement expired, content still inside its window
    with pytest.raises(contract.GalleryError, match="no current freshness statement"):
        trust.checked(record)
    trust.checked(record, historical=True)  # still inspectable


def test_gallery_short_cut_keeps_the_legacy_path(keys, gallery):
    index, accept, _clock, trust, _contract = gallery
    raw, doc, sig = index(long=False)
    trust.checked(accept(raw, doc, sig))


def test_gallery_renewal_reuse_rollback_and_equivocation(keys, gallery):
    index, accept, clock, trust, contract = gallery
    raw, doc, sig = index()
    first = bundle(keys, stmt(raw, doc, sequence=1, feed_name="gallery"))
    accept(raw, doc, sig, first)
    accept(raw, doc, sig, first)  # idempotent reuse
    clock["now"] = NOW + DAY
    renewed = stmt(
        raw, doc, sequence=2, feed_name="gallery", issued=NOW + DAY - 30, expires=NOW + 3 * DAY
    )
    record = accept(raw, doc, sig, bundle(keys, renewed))
    clock["now"] = NOW + 2 * DAY + DAY // 2
    trust.checked(record)
    at = clock["now"]
    for s in (  # each valid at this moment, so only the floor can refuse it
        stmt(raw, doc, sequence=1, feed_name="gallery", issued=at - 30, expires=at + DAY),
        stmt(raw, doc, sequence=2, feed_name="gallery", issued=at - 30, expires=at + DAY - 1),
    ):
        with pytest.raises(freshness.FreshnessError, match="backwards|different statement"):
            accept(raw, doc, sig, bundle(keys, s))
    trust.checked(record)


def test_gallery_pointer_ahead_of_the_cut_fails_closed_and_keeps_evidence(keys, gallery):
    index, accept, _clock, trust, contract = gallery
    raw, doc, sig = index(1)
    record = accept(raw, doc, sig, bundle(keys, stmt(raw, doc, feed_name="gallery")))
    raw2, doc2, sig2 = index(2)
    ahead = bundle(keys, stmt(raw2, doc2, sequence=2, feed_name="gallery"))
    with pytest.raises(contract.GalleryError, match="not current"):
        accept(raw, doc, sig, ahead)
    assert trust.current() == record
    trust.checked(accept(raw2, doc2, sig2, ahead))


# ---- shared vectors (byte-for-byte with battlelab-cloud#84) -------------------------------------

VECTORS = __import__("pathlib").Path(__file__).with_name("fixtures") / "freshness"


def test_shared_statement_vectors_are_frozen_and_parse_as_pinned():
    vectors = json.loads((VECTORS / "vectors.json").read_text())
    raw = (VECTORS / "statement.json").read_bytes()
    pointer = (VECTORS / "pointer.json").read_bytes()
    refusals = (VECTORS / "refusals.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == vectors["statement_sha256"]
    assert hashlib.sha256(pointer).hexdigest() == vectors["pointer_sha256"]
    assert hashlib.sha256(refusals).hexdigest() == vectors["refusals_sha256"]
    assert (vectors["namespace"], vectors["principal"]) == (
        freshness.NAMESPACE,
        freshness.PRINCIPAL,
    )
    assert vectors["max_statement_age"] == freshness.MAX_STATEMENT_AGE
    assert vectors["max_content_age"] == freshness.MAX_CONTENT_AGE
    parsed = freshness.parse(raw, historical=True)
    assert freshness.pointer(feed.decode(pointer)) == freshness.mark(parsed)
    for case in json.loads(refusals):
        with pytest.raises(freshness.FreshnessError):
            freshness.parse(case["raw"].encode(), historical=True)


def test_refresh_of_a_long_cut_fetches_its_statement_from_the_pinned_origin(keys, monkeypatch):
    from agent_sessions.plugins import feed_client

    monkeypatch.setattr(feed.time, "time", lambda: NOW)
    monkeypatch.setattr(freshness.time, "time", lambda: NOW)
    data, doc = cut()
    s = stmt(data, doc, sequence=3)
    s_digest = hashlib.sha256(s).hexdigest()
    tag = f"https://github.com/{feed_client.REPOSITORY}/releases/tag/v0.19.4"
    asset = (
        f"https://github.com/{feed_client.REPOSITORY}/releases/download/v0.19.4/plugin-feed.json"
    )
    fbase = freshness_client.BASE + "/agents"
    routes = {
        asset: data,
        asset + ".sig": keys.content(data),
        fbase + "/current.json": feed.canonical({"digest": s_digest, "sequence": 3}),
        f"{fbase}/releases/3-{s_digest}/statement.json": s,
        f"{fbase}/releases/3-{s_digest}/statement.json.sig": keys.statement(s),
    }
    seen = []

    def respond(request):
        url = str(request.url)
        seen.append(url)
        if url == feed_client.LATEST:
            return httpx.Response(302, headers={"location": tag})
        body = routes.get(url)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, stream=httpx.ByteStream(body))

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))
    snapshot = asyncio.run(feed_client.refresh())
    assert feed.fresh(snapshot, now=NOW)
    assert [u for u in seen if u.startswith(freshness_client.BASE)] == list(routes)[2:]


def test_a_stored_statement_behind_the_floor_is_not_fresh(keys):
    """An older (restored) record below the committed floor, though valid and unexpired, is a
    rollback: the long cut fails closed."""
    data, doc = cut()
    feed.accept(data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW)
    first = storage.read(ag_record(data))
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=2)), now=NOW
    )
    assert storage.read(ag_floor())["sequence"] == 2
    storage.write(ag_record(data), first)  # an older backup of the record
    assert not feed.fresh(feed.historical(), now=NOW)


def test_a_statement_that_outlives_its_content_is_refused(keys):
    doc = tpf.document(1)
    doc["issued_at"], doc["expires_at"] = NOW - 60, NOW + feed.MAX_AGE - 50  # long by 10 s
    data = feed.canonical(doc)
    s = stmt(data, doc, issued=NOW - 30, expires=NOW + feed.MAX_AGE - 31)  # 19 s past the content
    with pytest.raises(feed.FeedError, match="outlives"):
        feed.accept(data, keys.content(data), statement=bundle(keys, s), now=NOW)


# ---- damaged freshness trust records (Hermes 7180) -----------------------------------------------

from test_plugin_routes import api  # noqa: E402,F401 — the authenticated plugin routes


@pytest.mark.parametrize("record", ["kept", "emptied"])
def test_a_missing_statement_floor_refuses_an_older_replay_instead_of_resetting(keys, record):
    data, doc = cut()
    first = bundle(keys, stmt(data, doc, sequence=1))
    feed.accept(data, keys.content(data), statement=first, now=NOW)
    tomorrow = NOW + DAY
    renewed = stmt(data, doc, sequence=2, issued=tomorrow - 30, expires=tomorrow + 2 * DAY)
    feed.accept(data, keys.content(data), statement=bundle(keys, renewed), now=tomorrow)
    ag_floor().unlink()  # an incomplete restore
    if record == "emptied":  # damaged, but present: still not a first admission (Hermes 7185)
        storage.write(ag_record(data), {})
    before = storage.read(ag_record(data))
    # A record without its floor is damage, whether kept or emptied: the replay is refused.
    refused = (feed.FeedError, freshness.FreshnessError)
    with pytest.raises(refused, match="backwards|damaged|missing"):
        feed.accept(data, keys.content(data), statement=first, now=tomorrow)  # still valid
    assert storage.read(ag_record(data)) == before
    assert not ag_floor().exists()


@pytest.mark.parametrize("record", ["kept", "emptied"])
def test_gallery_missing_statement_floor_refuses_an_older_replay(keys, gallery, record):
    index, accept, clock, trust, contract = gallery
    raw, doc, sig = index()
    first = bundle(keys, stmt(raw, doc, sequence=1, feed_name="gallery"))
    accept(raw, doc, sig, first)
    clock["now"] = NOW + DAY
    renewed = stmt(
        raw, doc, sequence=2, feed_name="gallery", issued=NOW + DAY - 30, expires=NOW + 3 * DAY
    )
    accept(raw, doc, sig, bundle(keys, renewed))
    root = trust.storage.root()
    floor = root / f"{trust.STATEMENT}-floor.json"
    held = root / f"{trust.STATEMENT}-{hashlib.sha256(raw).hexdigest()}.json"
    floor.unlink()
    if record == "emptied":
        trust.storage.write(held, {})
    before = trust.storage.read(held)
    refused = (contract.GalleryError, freshness.FreshnessError)
    with pytest.raises(refused, match="backwards|damaged|missing"):
        accept(raw, doc, sig, first)
    assert trust.storage.read(held) == before
    assert not floor.exists()


def test_a_damaged_statement_floor_leaves_the_agent_list_available(keys, api, monkeypatch):  # noqa: F811
    from agent_sessions import agent_catalog

    data, doc = cut()
    feed.accept(data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW)
    with monkeypatch.context() as at:  # `time` is one module: unpatch before the signed-in call
        at.setattr(feed.time, "time", lambda: NOW)
        assert agent_catalog.current().stale is False
    storage.write(ag_floor(), {"damaged": True})
    catalog = agent_catalog.current()
    assert catalog.source == "unavailable" and catalog.error
    assert not any(c.source == "signed" for c in catalog.choices)
    client, headers, _ = api
    response = client.get("/api/agents/catalog", headers=headers)
    assert response.status_code == 200
    assert response.json()["feed"]["source"] == "unavailable"


# ---- a failed refresh never costs the cut in use (Hermes 7189) -----------------------------------


@pytest.mark.parametrize("before", ["short", "long"])
@pytest.mark.parametrize(
    "boundary,after",
    [
        # Write order of an admission: the new cut's statement, the content floor, the content
        # record, the statement floor. Each interruption leaves something usable, except the one
        # the content protocol has always failed closed on (floor ahead of record, pre-#1730).
        ("statement", "previous"),
        ("content-floor", "previous"),
        ("content-record", "closed"),
        # Written last when a floor exists (before = long), first when none does (short).
        ("statement-floor", "new-or-previous"),
    ],
)
def test_an_interrupted_agent_refresh_keeps_a_usable_cut(
    keys, monkeypatch, before, boundary, after
):
    monkeypatch.setattr(feed.time, "time", lambda: NOW)
    old, old_doc = cut(1, long=before == "long")
    feed.accept(
        old,
        keys.content(old),
        statement=bundle(keys, stmt(old, old_doc, sequence=1)) if before == "long" else None,
        now=NOW,
    )
    new, new_doc = cut(2)
    b = bundle(keys, stmt(new, new_doc, sequence=2))
    target = {
        "statement": ag_record(new).name,
        "content-floor": "feed-floor.json",
        "content-record": "feed.json",
        "statement-floor": ag_floor().name,
    }[boundary]
    real = storage.write

    def crash(path, value, **kw):
        if path.name == target:
            raise OSError("disk full")
        return real(path, value, **kw)

    monkeypatch.setattr(storage, "write", crash)
    with pytest.raises(OSError):
        feed.accept(new, keys.content(new), statement=b, now=NOW)
    monkeypatch.setattr(storage, "write", real)
    if after == "new-or-previous":
        after = "new" if before == "long" else "previous"
    if after == "closed":
        with pytest.raises(feed.FeedError):
            feed.current(now=NOW)
    else:
        usable = feed.current(now=NOW)
        assert usable.sequence == (1 if after == "previous" else 2)
    retried = feed.accept(new, keys.content(new), statement=b, now=NOW)  # a retry completes
    assert retried.sequence == 2 and feed.current(now=NOW).sequence == 2


@pytest.mark.parametrize("before", ["short", "long"])
@pytest.mark.parametrize(
    "boundary,after",
    [
        ("statement", "previous"),
        ("history", "previous"),
        ("floor", "previous"),
        ("index", "closed"),  # floor ahead of the index: the gallery's pre-#1730 fail-closed
        ("statement-floor", "new-or-previous"),
    ],
)
def test_an_interrupted_gallery_refresh_keeps_a_usable_cut(
    keys, gallery, monkeypatch, before, boundary, after
):
    index, accept, _clock, trust, contract = gallery
    raw1, doc1, sig1 = index(1, long=before == "long")
    first = bundle(keys, stmt(raw1, doc1, sequence=1, feed_name="gallery"))
    accept(raw1, doc1, sig1, first if before == "long" else None)
    raw2, doc2, sig2 = index(2)
    b = bundle(keys, stmt(raw2, doc2, sequence=2, feed_name="gallery"))
    digest2 = hashlib.sha256(raw2).hexdigest()
    target = {
        "statement": f"{trust.STATEMENT}-{digest2}.json",
        "history": f"cut-{digest2}.json",
        "floor": "floor.json",
        "index": "index.json",
        "statement-floor": f"{trust.STATEMENT}-floor.json",
    }[boundary]
    real = trust.storage.write

    def crash(path, value, **kw):
        if path.name == target:
            raise OSError("disk full")
        return real(path, value, **kw)

    monkeypatch.setattr(trust.storage, "write", crash)
    with pytest.raises(OSError):
        accept(raw2, doc2, sig2, b)
    monkeypatch.setattr(trust.storage, "write", real)
    if after == "new-or-previous":
        after = "new" if before == "long" else "previous"
    if after == "closed":
        with pytest.raises(contract.GalleryError):
            trust.checked(trust.current())
    else:
        record = trust.current()
        assert trust.checked(record).sequence == (1 if after == "previous" else 2)
    trust.checked(accept(raw2, doc2, sig2, b))  # a retry completes


def test_freshness_fetches_ask_for_identity_bytes_even_from_a_default_client():
    """The agent feed shares a default httpx client that advertises gzip/br. A server that
    honours that would send bytes the bounded identity reader refuses (Hermes 7189)."""
    import gzip

    data = b'{"x":1}'
    digest = hashlib.sha256(data).hexdigest()
    base = freshness_client.BASE + "/agents"
    routes = {
        base + "/current.json": feed.canonical({"digest": digest, "sequence": 7}),
        f"{base}/releases/7-{digest}/statement.json": data,
        f"{base}/releases/7-{digest}/statement.json.sig": b"sig",
    }
    asked = []

    def handler(request):
        asked.append(request.headers.get("accept-encoding"))
        body = routes[str(request.url)]
        if "gzip" in (request.headers.get("accept-encoding") or ""):
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=httpx.ByteStream(gzip.compress(body)),
            )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await freshness_client.fetch(client, "agents")

    assert asyncio.run(go()) == (data, b"sig", {"digest": digest, "sequence": 7})
    assert asked == ["identity"] * 3


def test_a_partial_restore_of_an_older_record_without_its_floor_is_never_authority(keys):
    """Hermes 7202: statements 1 then 2 for one long cut; a restore brings back only record 1 and
    loses the floor. That is damage: not fresh, and replaying statement 1 is refused."""
    data, doc = cut()
    first = bundle(keys, stmt(data, doc, sequence=1))
    feed.accept(data, keys.content(data), statement=first, now=NOW)
    old_record = storage.read(ag_record(data))
    tomorrow = NOW + DAY
    renewed = stmt(data, doc, sequence=2, issued=tomorrow - 30, expires=tomorrow + 2 * DAY)
    feed.accept(data, keys.content(data), statement=bundle(keys, renewed), now=tomorrow)
    storage.write(ag_record(data), old_record)
    ag_floor().unlink()
    assert not feed.fresh(feed.historical(), now=tomorrow)
    with pytest.raises(feed.FeedError, match="floor is missing"):
        feed.accept(data, keys.content(data), statement=first, now=tomorrow)
    assert not ag_floor().exists() and storage.read(ag_record(data)) == old_record


def test_gallery_partial_restore_of_an_older_record_without_its_floor(keys, gallery):
    index, accept, clock, trust, contract = gallery
    raw, doc, sig = index()
    first = bundle(keys, stmt(raw, doc, sequence=1, feed_name="gallery"))
    accept(raw, doc, sig, first)
    root = trust.storage.root()
    held = root / f"{trust.STATEMENT}-{hashlib.sha256(raw).hexdigest()}.json"
    floor = root / f"{trust.STATEMENT}-floor.json"
    old_record = trust.storage.read(held)
    clock["now"] = NOW + DAY
    renewed = stmt(
        raw, doc, sequence=2, feed_name="gallery", issued=NOW + DAY - 30, expires=NOW + 3 * DAY
    )
    record = accept(raw, doc, sig, bundle(keys, renewed))
    trust.storage.write(held, old_record)
    floor.unlink()
    with pytest.raises(contract.GalleryError):
        trust.checked(record)
    with pytest.raises(contract.GalleryError, match="floor is missing"):
        accept(raw, doc, sig, first)
    assert not floor.exists() and trust.storage.read(held) == old_record


# ---- independent review of 473a7880 --------------------------------------------------------------


def _pin_only(tmp, key):
    """A rotation release: freshness-signers lists only `key`."""
    allowed = tmp / f"signers-{key.name}"
    allowed.write_text(freshness.PRINCIPAL + " " + key.with_suffix(".pub").read_text())
    return allowed


def test_a_stolen_freshness_key_cannot_disable_long_cuts_past_the_next_release(keys):
    """D1: a forged statement at the maximum sequence for the cut in use outranks every genuine
    statement for THAT cut, but a new release-approved cut always outranks it."""
    data, doc = cut(1)
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=5)), now=NOW
    )
    poisoned = stmt(data, doc, sequence=freshness._MAX_INT)
    feed.accept(data, keys.content(data), statement=bundle(keys, poisoned), now=NOW)
    with pytest.raises(freshness.FreshnessError, match="backwards"):
        feed.accept(
            data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=6)), now=NOW
        )
    recut, recut_doc = cut(2)  # the operator's response: a new release cut
    snapshot = feed.accept(
        recut,
        keys.content(recut),
        statement=bundle(keys, stmt(recut, recut_doc, sequence=7)),
        now=NOW,
    )
    assert feed.fresh(snapshot, now=NOW)


def test_a_rotated_freshness_key_recovers_with_the_new_keys_next_statement(keys, monkeypatch):
    """D2: after a rotation release pins only the new key, the old key's retained record is no
    longer authority (not fresh) but not damage either: the new key's statement is admitted."""
    data, doc = cut(1)
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=3)), now=NOW
    )
    new_key = _key(keys.tmp, "freshness-2")
    monkeypatch.setattr(freshness, "SIGNERS", _pin_only(keys.tmp, new_key))
    assert not feed.fresh(feed.historical(), now=NOW)
    renewed = stmt(data, doc, sequence=4)
    b = bundle(keys, renewed, signature=keys.statement(renewed, new_key))
    snapshot = feed.accept(data, keys.content(data), statement=b, now=NOW)
    assert feed.fresh(snapshot, now=NOW)


def test_a_gallery_rotation_recovers_too(keys, gallery, monkeypatch):
    index, accept, _clock, trust, _contract = gallery
    raw, doc, sig = index()
    record = accept(raw, doc, sig, bundle(keys, stmt(raw, doc, sequence=3, feed_name="gallery")))
    new_key = _key(keys.tmp, "freshness-g2")
    monkeypatch.setattr(freshness, "SIGNERS", _pin_only(keys.tmp, new_key))
    with pytest.raises(_contract.GalleryError):
        trust.checked(record)
    renewed = stmt(raw, doc, sequence=4, feed_name="gallery")
    record = accept(
        raw, doc, sig, bundle(keys, renewed, signature=keys.statement(renewed, new_key))
    )
    trust.checked(record)


def test_an_unsafe_gallery_freshness_file_degrades_instead_of_answering_500(keys, gallery):
    """D3: a freshness file the storage layer refuses (wrong mode) is the gallery's own error,
    so the listing reports it and import refuses it, never a 500."""
    from agent_sessions.gallery import library

    index, accept, _clock, trust, contract = gallery
    raw, doc, sig = index()
    accept(raw, doc, sig, bundle(keys, stmt(raw, doc, feed_name="gallery")))
    (trust.storage.root() / f"{trust.STATEMENT}-floor.json").chmod(0o666)
    listing = library.catalog()
    assert listing["state"] in ("stale", "unavailable")
    with pytest.raises(contract.GalleryError):
        trust.checked(trust.current())


def test_a_record_filed_under_another_cuts_digest_is_damage(keys):
    data, doc = cut(1)
    feed.accept(data, keys.content(data), statement=bundle(keys, stmt(data, doc)), now=NOW)
    ag_record(data).rename(ag_record(b"another cut"))
    with pytest.raises(feed.FeedError, match="damaged"):
        feed.accept(
            data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=2)), now=NOW
        )


def test_a_short_cut_ignores_a_statement_it_cannot_use(keys):
    """A short cut needs no statement: one the freshness records refuse (here a damaged
    retained record) is ignored, and the short cut is accepted on its own window."""
    long_data, long_doc = cut(1)
    feed.accept(
        long_data,
        keys.content(long_data),
        statement=bundle(keys, stmt(long_data, long_doc, sequence=9)),
        now=NOW,
    )
    storage.write(ag_record(long_data), {})  # damaged retained record
    short, short_doc = cut(2, long=False)
    offered = stmt(short, short_doc, sequence=10, expires=NOW + DAY // 2)
    snapshot = feed.accept(short, keys.content(short), statement=bundle(keys, offered), now=NOW)
    assert snapshot.sequence == 2 and feed.fresh(snapshot, now=NOW)
    with pytest.raises(feed.FeedError, match="damaged"):  # a long cut is still refused
        later, later_doc = cut(3)
        feed.accept(
            later,
            keys.content(later),
            statement=bundle(keys, stmt(later, later_doc, sequence=11)),
            now=NOW,
        )


def test_an_older_floor_restored_beside_a_newer_record_still_bounds_the_replay(keys):
    """M3: the retained record raises the effective floor above a restored older floor file."""
    data, doc = cut(1)
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=1)), now=NOW
    )
    old_floor = storage.read(ag_floor())
    feed.accept(
        data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=2)), now=NOW
    )
    storage.write(ag_floor(), old_floor)
    with pytest.raises(freshness.FreshnessError, match="backwards"):
        feed.accept(
            data, keys.content(data), statement=bundle(keys, stmt(data, doc, sequence=1)), now=NOW
        )


def test_a_statement_that_predates_its_cut_is_refused(keys):
    """M5: a statement issued before the cut it names can't vouch for it."""
    data, doc = cut(1)  # issued NOW - 60
    early = stmt(data, doc, issued=NOW - 120)
    with pytest.raises(feed.FeedError, match="predates"):
        feed.accept(data, keys.content(data), statement=bundle(keys, early), now=NOW)


def test_the_long_cut_boundary_is_exactly_the_legacy_window():
    """M10: a window of exactly MAX_AGE is short; one second more is long."""
    assert not freshness.is_long(NOW, NOW + feed.MAX_AGE)
    assert freshness.is_long(NOW, NOW + feed.MAX_AGE + 1)


def test_a_compressed_answer_is_refused_even_after_asking_for_identity():
    """M14: a server that compresses anyway gets no statement accepted."""
    import gzip

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=httpx.ByteStream(gzip.compress(b'{"digest":"' + b"a" * 64 + b'","sequence":1}')),
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await freshness_client.fetch(client, "agents")

    assert asyncio.run(go()) is None
