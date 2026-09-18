"""#1007 Phase 3: a paging sequence pins ONE disk walk — and nothing else.

The map pages ``/api/sessions`` to completion. A mutation that lands between two pages calls
``invalidate_scan_cache()``, so the next page paid a second cold walk (measured 6.82 s for the
sequence against 3.46 s). Page 1 now asks for ``snapshot=new``, later pages pass the returned token
back, and the whole sequence is served from the walk page 1 used.

What these tests hold, in order of importance:

* **The pin is never an authorization cache (#991).** Scope — ``folder_exclusions``, archived
  state — is re-applied to the pinned rows on every request. A test that only checked the second
  page's rows could pass by re-walking, so each one also asserts that NO walk happened: the rows
  came from the pin AND reflect the new scope.
* **One walk, not a frozen order.** Metadata is reloaded per request, so a rename, favourite or
  archive between pages reorders the sort, and a row can be skipped or served twice. That is the
  accepted contract and is asserted explicitly, so nobody reads Phase 3 as promising more.
* **Anything that is not a live token is a normal scan**, never an error and never empty.
* **No token, no change**: the sidebar's requests neither read nor fill the pin store.
* **Bounded**: a fixed lifetime that use cannot extend, and a count past which the oldest goes.

Walks are counted through the ``engines.scan_all`` seam, never timed. Time is a fake monotonic clock
on the registry module only, so the scan TTL and the pin lifetime move together and nothing sleeps.

Every token is read with ``.get``: on a tree without the pin there is no token, and these tests must
then fail on the walk count they exist to guard — not on a ``KeyError``.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_sessions.routes.sessions as sessions_routes
from agent_sessions import engines, prefs
from agent_sessions.engines import registry
from agent_sessions.main import create_app
from agent_sessions.scanner import Session

_KEEP = "/work/keep"
_SECRET = "/work/secret"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _sess(n: int, cwd: str = _KEEP) -> Session:
    # A sidecar-only engine, so the REAL archive route works against a stubbed walk. A higher
    # ``n`` is newer, so the list's default order is n descending.
    return Session(
        engine="shell",
        uuid=_uuid(n),
        cwd=cwd,
        last_mtime=1_000.0 + n,
        first_user_message=f"session {n}",
        archived=False,
        created_at=1_000.0 + n,
    )


def _key(n: int) -> str:
    return f"shell:{_uuid(n)}"


class _Disk:
    """The on-disk session set, per home, behind a ``scan_all`` stub that counts real walks."""

    def __init__(self) -> None:
        self.walks = 0
        self.by_home: dict[str, list[Session]] = {}

    def put(self, *sessions: Session) -> None:
        self.by_home.setdefault(str(Path.home()), []).extend(sessions)

    def scan_all(self) -> list[Session]:
        self.walks += 1
        return list(self.by_home.get(str(Path.home()), []))


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def disk(monkeypatch, tmp_home) -> _Disk:
    d = _Disk()
    monkeypatch.setattr(engines, "scan_all", d.scan_all)
    # The production TTL, so the ordinary snapshot really is warm between pages and the only thing
    # that can force a second walk is what these tests do on purpose.
    engines.set_scan_cache_ttl(10.0)
    return d


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(registry, "time", types.SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture
def api(auth_cfg, disk, monkeypatch):
    async def _no_runtime(*_a, **_k):  # nothing to reap; never reach a real process group
        return "gone"

    monkeypatch.setattr(sessions_routes.runtime_cleanup, "cleanup_runtime", _no_runtime)
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    c.headers.update({"X-CSRF-Token": csrf, "Origin": auth_cfg.origin})
    return c


def _list(c, *, snapshot: str | None = None, **params) -> dict:
    params = {"limit": 200, "offset": 0, **params}
    if snapshot is not None:
        params["snapshot"] = snapshot
    r = c.get("/api/sessions", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _token(body: dict) -> str:
    return body.get("snapshot") or "<no token>"


def _ns(body: dict) -> list[int]:
    return [int(s["uuid"].rsplit("-", 1)[1]) for s in body["sessions"]]


# ---- one walk per sequence ------------------------------------------------------------------


def test_an_invalidation_between_pages_costs_one_walk_not_two(api, disk, clock):
    """THE defect: a mutating route invalidates the scan cache mid-sequence, and the next page
    re-walked. With the pin, the whole sequence is one walk."""
    disk.put(*(_sess(n) for n in range(1, 6)))
    disk.walks = 0

    page1 = _list(api, snapshot="new", limit=2)
    assert disk.walks == 1
    token = _token(page1)

    engines.invalidate_scan_cache()  # what archive / unarchive / a launch does
    page2 = _list(api, snapshot=token, limit=2, offset=2)
    engines.invalidate_scan_cache()
    page3 = _list(api, snapshot=token, limit=2, offset=4)

    assert disk.walks == 1, "an invalidation between pages forced a second cold walk"
    assert _ns(page1) + _ns(page2) + _ns(page3) == [5, 4, 3, 2, 1]
    assert page3["next_offset"] is None

    # Control: the invalidation was real — a request that did not pin pays for it.
    _list(api, limit=2)
    assert disk.walks == 2


def test_a_scan_ttl_expiry_between_pages_costs_one_walk_not_two(api, disk, clock):
    disk.put(*(_sess(n) for n in range(1, 5)))
    disk.walks = 0

    token = _token(_list(api, snapshot="new", limit=2))
    assert disk.walks == 1

    clock.now += 11.0  # past the 10 s scan TTL: the ordinary snapshot is cold again
    page2 = _list(api, snapshot=token, limit=2, offset=2)
    assert disk.walks == 1, "a TTL expiry between pages forced a second cold walk"
    assert _ns(page2) == [2, 1]

    _list(api, limit=2)  # control: the ordinary cache really had expired
    assert disk.walks == 2


# ---- #991: the pin is a performance cache, never an authorization cache ----------------------


def test_folder_exclusions_changed_between_pages_apply_under_the_same_token(api, disk, clock):
    """The #991 boundary. The pinned walk still holds the excluded rows — only the per-request
    scope keeps them out, and it must do so on the very next request under the same token. Asserted
    together with `walks == 1`, because a page that re-walked would pass the row check for the
    wrong reason."""
    disk.put(
        _sess(1, _KEEP), _sess(2, _SECRET), _sess(3, _KEEP), _sess(4, _SECRET), _sess(5, _KEEP)
    )
    disk.walks = 0

    page1 = _list(api, snapshot="new", limit=2)
    token = _token(page1)
    assert _ns(page1) == [5, 4]  # 4 is under /work/secret and was in scope for page 1
    assert page1["total"] == 5

    prefs.set_folder_exclusions([_SECRET])
    # Invalidate too, so `walks == 1` below can ONLY mean the pin served the page. Without it the
    # ordinary 10 s snapshot is still warm and would satisfy the count on a tree with no pin at all.
    engines.invalidate_scan_cache()

    page2 = _list(api, snapshot=token, limit=2, offset=2)
    assert disk.walks == 1  # served from the pin...
    assert all(s["cwd"] != _SECRET for s in page2["sessions"])  # ...under the NEW scope
    assert page2["total"] == 3
    assert _ns(page2) == [1]

    # The whole set under the same token: the excluded rows are gone, not merely off this window.
    whole = _list(api, snapshot=token)
    assert disk.walks == 1
    assert _ns(whole) == [5, 3, 1]

    # And the other direction: lifting the exclusion restores them on the next request, too.
    prefs.set_folder_exclusions([])
    engines.invalidate_scan_cache()
    whole = _list(api, snapshot=token)
    assert disk.walks == 1
    assert _ns(whole) == [5, 4, 3, 2, 1]


def test_archiving_between_pages_applies_under_the_same_token(api, disk, clock):
    disk.put(*(_sess(n) for n in range(1, 5)))
    disk.walks = 0

    token = _token(_list(api, snapshot="new", limit=2))

    r = api.post(f"/api/sessions/{_key(2)}/archive")
    assert r.status_code == 200, r.text

    live = _list(api, snapshot=token)
    archived = _list(api, snapshot=token, archived=1)
    assert disk.walks == 1  # both served from the pin, although archive invalidated the cache
    assert _ns(live) == [4, 3, 1]  # the archived row left the active list...
    assert _ns(archived) == [2]  # ...and is in the archived one: the flag is read per request


# ---- decision 1: ONE walk, NOT a frozen order ------------------------------------------------


def test_an_archive_between_pages_can_skip_a_row_the_pin_does_not_freeze_order(api, disk, clock):
    """Phase 3 guarantees one walk per sequence, NOT a coherent ordering. The archive removes a row
    page 1 already served, the window shifts, and one live row is served on NEITHER page. Pinned so
    that nobody later reads the snapshot as a frozen result."""
    disk.put(*(_sess(n) for n in range(1, 6)))
    disk.walks = 0

    page1 = _list(api, snapshot="new", limit=2)
    assert _ns(page1) == [5, 4]
    assert api.post(f"/api/sessions/{_key(5)}/archive").status_code == 200
    page2 = _list(api, snapshot=_token(page1), limit=2, offset=2)

    assert disk.walks == 1  # same walk throughout...
    assert _ns(page2) == [2, 1]  # ...but the order is recomputed per request
    served = set(_ns(page1)) | set(_ns(page2))
    assert 3 not in served  # a live session the sequence never served


def test_a_favourite_between_pages_reorders_the_pin_does_not_freeze_order(api, disk, clock):
    disk.put(*(_sess(n) for n in range(1, 6)))
    disk.walks = 0

    page1 = _list(api, snapshot="new", limit=2)
    assert _ns(page1) == [5, 4]
    assert api.post(f"/api/sessions/{_key(1)}/favorite").status_code == 200
    # A favourite is a sidecar write and does not invalidate; something else does, so that
    # `walks == 1` proves the page came from the pin rather than a still-warm ordinary snapshot.
    engines.invalidate_scan_cache()
    page2 = _list(api, snapshot=_token(page1), limit=2, offset=2)

    assert disk.walks == 1
    # Favourites lead the sort, so 1 jumped to the front and everything shifted down one: 4 is
    # served twice, and 1 — now on page 1 — was never served at all.
    assert _ns(page2) == [4, 3]


def test_a_rename_between_pages_is_live_the_pin_does_not_freeze_metadata(api, disk, clock):
    disk.put(*(_sess(n) for n in range(1, 5)))
    disk.walks = 0

    page1 = _list(api, snapshot="new", limit=2)
    r = api.post(f"/api/sessions/{_key(2)}/rename", json={"title": "renamed mid-sequence"})
    assert r.status_code == 200
    engines.invalidate_scan_cache()  # as above: `walks == 1` must mean the pin, not the TTL cache
    page2 = _list(api, snapshot=_token(page1), limit=2, offset=2)

    assert disk.walks == 1
    assert {s["uuid"]: s["title"] for s in page2["sessions"]}[_uuid(2)] == "renamed mid-sequence"


# ---- anything that is not a live token is an ordinary scan -----------------------------------


@pytest.mark.parametrize(
    "sent",
    [
        "new",
        "",
        "not-a-token",
        "AAAAAAAAAAAAAAAAAAAAAA",  # the minted shape, never minted
        "x" * 4096,
        "../../etc/passwd",
        "☃",
    ],
)
def test_an_unknown_or_malformed_token_falls_back_to_a_normal_scan(api, disk, clock, sent):
    disk.put(_sess(1), _sess(2))
    _list(api)  # a warm ordinary snapshot...
    disk.put(_sess(3))
    engines.invalidate_scan_cache()  # ...made stale by a mutation, as a route would
    disk.walks = 0

    body = _list(api, snapshot=sent)
    assert disk.walks == 1  # a real, fresh walk — not a pin, not the stale snapshot
    assert _ns(body) == [3, 2, 1]  # correct rows: never empty, never an error
    minted = _token(body)
    assert minted != sent  # always server-minted; the input is never echoed back
    assert len(minted) >= 22

    # The minted token is a working pin for the rest of the sequence.
    engines.invalidate_scan_cache()
    _list(api, snapshot=minted, offset=1)
    assert disk.walks == 1


def test_a_pin_expires_on_its_own_and_use_never_extends_it(api, disk, clock):
    disk.put(_sess(1), _sess(2))
    disk.walks = 0

    token = _token(_list(api, snapshot="new"))
    assert disk.walks == 1

    # Used right up to the edge of the 60 s lifetime, across invalidations...
    # (Steps are exact in binary floating point, so "exactly 60 s" below really is 60.0.)
    for dt in (20.0, 20.0, 19.5):
        clock.now += dt
        engines.invalidate_scan_cache()
        assert _ns(_list(api, snapshot=token)) == [2, 1]
    assert disk.walks == 1  # ...it was served every time, and none of those uses extended it.

    disk.put(_sess(3))
    clock.now += 0.5  # exactly 60 s after the mint
    body = _list(api, snapshot=token)
    assert disk.walks == 2  # expired → an ordinary scan, which walks (the cache was invalidated)
    assert _ns(body) == [3, 2, 1]  # fresh rows, not the pinned ones
    assert _token(body) != token


def test_a_token_minted_under_another_home_is_unknown(api, disk, clock, monkeypatch, tmp_path):
    disk.put(_sess(1))
    token = _token(_list(api, snapshot="new"))

    other = tmp_path / "other-home"
    other.mkdir()
    monkeypatch.setenv("HOME", str(other))
    disk.put(_sess(7))
    disk.walks = 0

    body = _list(api, snapshot=token)
    assert disk.walks == 1
    assert _ns(body) == [7]  # this home's walk, never the pin minted under the first one
    assert _token(body) != token


# ---- no token: the sidebar's path is untouched -----------------------------------------------


def test_a_request_without_a_token_neither_reads_nor_fills_the_pin_store(api, disk, clock):
    disk.put(_sess(1), _sess(2))
    disk.walks = 0

    token = _token(_list(api, snapshot="new"))
    assert disk.walks == 1

    disk.put(_sess(3))
    engines.invalidate_scan_cache()

    # The sidebar: an ordinary cached read, as before this change. It re-walks after the
    # invalidation, sees the new session, and carries no token.
    sidebar = _list(api, limit=20)
    assert disk.walks == 2
    assert _ns(sidebar) == [3, 2, 1]
    assert "snapshot" not in sidebar
    _list(api, limit=20)  # and within the TTL it is the ordinary warm snapshot
    assert disk.walks == 2

    # Its requests take no pin slots: after far more of them than the bound, the pin still serves
    # the walk it was minted on — the one without session 3.
    for _ in range(3 * 8):
        _list(api, limit=20, offset=0)
    engines.invalidate_scan_cache()
    assert _ns(_list(api, snapshot=token)) == [2, 1]
    assert disk.walks == 2


def test_the_terminal_authorization_walk_never_reads_a_pin(api, disk, clock):
    """``scan_all_since`` is what the terminal route authorizes an attach/resume against (#991). A
    pin is a listing optimisation and must never answer it."""
    disk.put(_sess(1))
    _list(api, snapshot="new")
    disk.put(_sess(2))
    disk.walks = 0

    clock.now += 1.0  # a connect arriving after the pin was minted, well inside its lifetime
    rows = engines.scan_all_since(clock.monotonic())
    assert disk.walks == 1
    assert {s.uuid for s in rows} == {_uuid(1), _uuid(2)}


# ---- the bound -------------------------------------------------------------------------------


def test_minting_past_the_bound_evicts_the_oldest_pin(api, disk, clock):
    disk.put(_sess(1), _sess(2))
    disk.walks = 0

    tokens = [_token(_list(api, snapshot="new")) for _ in range(8 + 1)]
    assert disk.walks == 1  # every mint rode the one warm snapshot
    disk.put(_sess(3))
    engines.invalidate_scan_cache()

    # The newest eight are live — checked first, since serving a live token mints nothing.
    for t in tokens[1:]:
        assert _ns(_list(api, snapshot=t)) == [2, 1]
    assert disk.walks == 1

    # The oldest was evicted by the ninth mint: an ordinary scan, which walks.
    body = _list(api, snapshot=tokens[0])
    assert disk.walks == 2
    assert _ns(body) == [3, 2, 1]


def test_pins_from_one_walk_share_it_rather_than_copying_it(api, disk, clock):
    """Memory is bounded by distinct WALKS, not by tokens: many tabs opening the map inside one
    scan TTL hold one list between them."""
    disk.put(_sess(1), _sess(2))
    a = _token(_list(api, snapshot="new"))
    b = _token(_list(api, snapshot="new"))
    assert a != b
    assert registry._pins[a].sessions is registry._pins[b].sessions
    assert len(registry._pins) <= registry._SNAPSHOT_MAX
