"""Artifacts (#1932 P1): files and static sites an agent hosts on BattleLab, at a stable link.

An **artifact** is a server-owned snapshot. ``artifact_publish`` (the `artifacts_mcp` tool, judged
here through `artifact_broker`) **copies** a file or a directory out of the session's workspace
into BattleLab's own store, so editing the workspace afterwards changes nothing until the agent
publishes again. Publishing again with the artifact's id adds a **revision** behind the same link.

The rules (pinned in ``docs/invariants/artifacts.md``):

* **Only the session's own folders.** A path is judged with the mission shelf's boundary
  (`mission_shelf.Boundary`): inside the session's cwd, its project's folders or (when a mission
  holds it) the mission's folders, AND inside the configured roots and outside
  `folder_exclusions`. Every file is opened through `mission_shelf.open_verified` (no final
  symlink, a regular file, the scope re-proved on the descriptor). A site's tree may hold **no
  symlink at all** (refused, never followed). A hidden path, a key file and an archive are refused
  by name; inside a site, dot-entries are skipped.
* **Withheld, never redacted.** Every file whose head is text is judged by `secret_shaped` on the
  STAGED bytes (what will be served); one credential value refuses the whole publish and the agent
  hears which path.
* **Bounded.** `REVISION_BYTES_MAX` and `REVISION_FILES_MAX` per revision, `REVISIONS_MAX` per
  artifact (the oldest pruned, never the latest), `TOTAL_BYTES_MAX` for the whole store.
* **Immutable, content-addressed.** A revision is a manifest ``path → sha256, size, type``; its
  bytes are blobs named by their sha256 (identical files stored once). Bytes are staged privately
  and become blobs only inside the commit, under the write lock: a refused publish leaves nothing,
  and a delete removes its blobs at once.
* **Served by manifest lookup only.** A request path is looked up in the revision's manifest; the
  filesystem is never joined with a client path. Serving and its sandbox headers are
  `routes/artifacts.py`.

The store is a 0600 SQLite file (``AGENT_SESSIONS_ARTIFACTS_DB``) and a 0700 content directory
(``AGENT_SESSIONS_ARTIFACTS_DIR``), the `shares.db` discipline.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import artifacts_mcp, mission_shelf, prefs, project_dirs

log = logging.getLogger(__name__)

KINDS: tuple[str, ...] = ("file", "site")
MODES: tuple[str, ...] = ("off", "private", "public")
SCOPES: tuple[str, ...] = ("session", "project", "mission")
#: What a session gets when nothing is set for it, its mission or its project (the operator's
#: call, 2026-11-12: on in every session). PRIVATE: the agent publishes, only the operator sees and
#: shares; an agent sharing publicly itself still needs an explicit PUBLIC.
DEFAULT_MODE = "private"
VISIBILITIES: tuple[str, ...] = ("private", "public")

TITLE_MAX = mission_shelf.TITLE_MAX
WHY_MAX = mission_shelf.WHY_MAX
PATH_MAX = mission_shelf.PATH_MAX
#: One site path segment, and a whole relative path, as served.
SEGMENT_MAX = 255
REL_PATH_MAX = 1024

REVISION_BYTES_MAX = int(os.environ.get("AGENT_SESSIONS_ARTIFACT_REVISION_MAX") or 200 * 2**20)
REVISION_FILES_MAX = 2000
REVISIONS_MAX = 20
TOTAL_BYTES_MAX = int(os.environ.get("AGENT_SESSIONS_ARTIFACTS_TOTAL_MAX") or 5 * 2**30)
#: A text file is judged for credentials whole; one larger than this cannot be judged and is
#: refused (strictly safer than publishing it unjudged).
TEXT_CHECK_MAX = 8 * 2**20
PAGE_MAX = 100
#: A repository named in a publish: its name (at most 64 characters) or its id.
REPO_REF_MAX = 64
_CHUNK = 1024 * 1024

#: An artifact id: four groups of four digits (~52 bits), e.g. ``5449-5592-2107-4372``. It names a
#: PRIVATE artifact (sign-in required), and it travels in links HQ answers with and the mission
#: shelf stores — both judged by the chat credential guard (`ask_store.guard_text`), which reads any
#: long random alphanumeric run as a token. Digit groups are never read as one; unguessability that
#: a bearer link needs is the PUBLIC id's job (P3).
ID_RE = re.compile(r"^[1-9][0-9]{3}(?:-[1-9][0-9]{3}){3}$")
PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

#: Refusal codes. `artifacts_mcp.MEANING` IS this table (one source of truth, import-light there).
REFUSALS: frozenset[str] = frozenset(artifacts_mcp.MEANING)

#: Served types, by extension: a fixed table, never sniffed from content and never taken from the
#: agent. Anything else is served as a download (`routes/artifacts.py`).
TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".xml": "text/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
}
#: Judged for credentials before a publish (decoded as UTF-8, errors replaced).
TEXT_EXTS: frozenset[str] = frozenset(
    {
        ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".map",
        ".txt", ".md", ".csv", ".tsv", ".log", ".xml", ".svg", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".conf", ".env", ".py", ".sh", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h",
        ".cpp", ".sql", ".php", ".vue", ".svelte", ".tex", ".rst", ".ipynb",
    }
)  # fmt: skip


#: Archives are refused by name: their members would escape every rule (hidden paths, key files,
#: credential values). Publish the folder instead.
ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".jar", ".war",
)  # fmt: skip
#: Key and keystore files are refused by NAME, whatever their bytes (a binary keystore never
#: decodes as text, so the text judge cannot see it).
CREDENTIAL_EXTS: frozenset[str] = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".ppk", ".asc", ".gpg", ".jks", ".keystore", ".kdbx"}
)
CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
        "credentials", "htpasswd", "netrc", "pgpass", "authorized_keys", "known_hosts",
    }
)  # fmt: skip
_TEXT_HEAD = 8192

# ---- the file judge: credential VALUES, not credential-ish prose ------------------------------
# Deliberately NOT `template_suggest.credential_shaped` (the chat guard): it withholds a message on
# any "credential word + separator" or long random run, so it refuses ordinary website code
# (`fetch(url, {credentials: "include"})`, every minified bundle). Like `variables.
# label_value_shaped` (#1786), a file is refused for a stored secret or for something that IS a
# credential value. Measured on 12,097 files of web/node_modules plus the app's own build: 10
# flagged, all but one a URL carrying `user:password@`.
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
#: A credential-NAMED key: a credential word anywhere in it, or a bare ``key`` / ``pass`` / ``auth``
#: as its last part (``STRIPE_KEY``, ``DB_PASS``). Its value must still look like a secret.
_CRED_KEY_RE = re.compile(
    r"(?i)secret|token|passw|pwd|credential|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|signing[_-]?key|encryption[_-]?key|webhook|(?:^|[_.-])(?:key|pass|auth)$"
)
#: An env / ini / yaml line: ``KEY = value``.
_CRED_LINE_RE = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?([\w.-]+)[ \t]*[:=][ \t]*[\"']?"
    r"([^\s\"'#;()\[\]{}]{12,})[\"']?[ \t]*(?:#.*)?$"
)
#: Code: ``key = "value"`` / ``key: 'value'`` — one separator (never ``==``), a CLOSED quote.
_CRED_CODE_RE = re.compile(
    r"""(?i)\b([\w$]+)["']?[ \t]*(?<![=!<>])[:=](?!=)[ \t]*(["'`])([^"'`\s]{12,300})\2"""
)
#: Credential values that need no key name: a bearer token, a Slack webhook, an npm auth token,
#: a Docker config's base64 ``auth``, a kubeconfig's embedded client key.
_CRED_VALUE_RE = re.compile(
    r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{20,}"
    r"|hooks\.slack\.com/services/T[A-Z0-9]+/"
    r"|_authToken[ \t]*=[ \t]*\S{8,}"
    r"|\"auth\"[ \t]*:[ \t]*\"[A-Za-z0-9+/=]{16,}\""
    r"|client-key-data:[ \t]*\S{40,}"
)
_HEX_RE = re.compile(r"[0-9a-fA-F]{24,}")
_CODE_CHARS_RE = re.compile(r"[$|\[\]\\(){}<>]")
_B64ISH_RE = re.compile(r"[A-Za-z0-9+/=_.-]{24,}")


def _secret_value(value: str) -> bool:
    """Does a credential-named key's value look like a secret: random-looking, a long hex run
    (``token_hex``), or a long base64-like run mixing letters and digits?"""
    from . import template_suggest

    if template_suggest._looks_random(value) or _HEX_RE.fullmatch(value):
        return True
    return bool(
        _B64ISH_RE.fullmatch(value)
        and re.search(r"[0-9]", value)
        and re.search(r"[A-Za-z]", value)
        and not value.startswith(("http", "./", "../", "/"))
    )


def secret_shaped(text: str) -> bool:
    """Does a file's text hold a credential VALUE: a stored secret, a private key, URL userinfo,
    a known token prefix, a bearer token or other keyless credential, or a credential-named key
    assigned a secret-looking value? Raises `ArtifactRefused` ``store_unavailable`` when the stored
    secrets cannot be read (fail closed). Not caught: a low-entropy value (``password1234``)."""
    from . import template_secrets, template_suggest, variables

    try:
        if template_secrets.redact_text(text) != text:
            return True
    except Exception:  # noqa: BLE001 — redaction that cannot run authorizes nothing
        raise ArtifactRefused("store_unavailable", status=503) from None
    if (
        _PRIVATE_KEY_RE.search(text)
        or template_suggest._USERINFO_RE.search(text)
        or variables._TOKEN_PREFIX_RE.search(text)
        or _CRED_VALUE_RE.search(text)
    ):
        return True
    for rx, key_group, value_group in ((_CRED_LINE_RE, 1, 2), (_CRED_CODE_RE, 1, 3)):
        for m in rx.finditer(text):
            key, value = m.group(key_group), m.group(value_group)
            if not _CRED_KEY_RE.search(key) or key.lower() in value.lower():
                continue  # not a credential name, or a generated one (`chipToken: "_chipToken_3"`)
            if _CODE_CHARS_RE.search(value):
                continue  # a template literal or a regex (`key = `${id}-${n}``), never a secret
            if _secret_value(value):
                return True
    return False


class ArtifactRefused(Exception):
    """A refusal an agent hears (`code` ∈ `REFUSALS`), optionally naming the offending path."""

    def __init__(self, code: str, path: str | None = None, *, status: int = 422) -> None:
        assert code in REFUSALS, code
        super().__init__(code)
        self.code = code
        self.path = path
        self.status = status


class ArtifactsUnavailable(Exception):
    """The store cannot be read or written (the operator API answers 503)."""


# ---------------------------------------------------------------- paths + connection


def db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_ARTIFACTS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "artifact_store.db"),
        )
    )


def content_dir() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_ARTIFACTS_DIR",
            str(Path.home() / ".local" / "share" / "agent-sessions" / "artifacts"),
        )
    )


#: 2: shared artifact repositories (#2347 P1, `repo_store`): their rows live here, beside the
#: artifacts whose blobs they reference.
SCHEMA_VERSION = 2
DDL = """
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  why TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file','site')),
  engine TEXT NOT NULL,
  session_key TEXT NOT NULL,
  phys_key TEXT NOT NULL,
  cwd TEXT NOT NULL,
  project_id TEXT,
  mission_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  latest INTEGER NOT NULL,
  public_id TEXT UNIQUE,
  public_since REAL,
  public_password_hash TEXT,
  public_expires_at REAL,
  public_header INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS artifacts_session ON artifacts(session_key);
CREATE INDEX IF NOT EXISTS artifacts_updated ON artifacts(updated_at);
CREATE TABLE IF NOT EXISTS artifact_revisions (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  n INTEGER NOT NULL,
  created_at REAL NOT NULL,
  files INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  entry TEXT,
  manifest TEXT NOT NULL,
  PRIMARY KEY (artifact_id, n)
);
CREATE TABLE IF NOT EXISTS artifact_activation (
  scope TEXT NOT NULL CHECK (scope IN ('session','project','mission')),
  scope_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('off','private','public')),
  updated_at REAL NOT NULL,
  PRIMARY KEY (scope, scope_id)
);
CREATE TABLE IF NOT EXISTS artifact_caps (
  id TEXT PRIMARY KEY,
  digest TEXT NOT NULL UNIQUE,
  engine TEXT NOT NULL,
  session_key TEXT NOT NULL,
  phys_key TEXT NOT NULL,
  cwd TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('private','public')),
  created_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS artifact_caps_phys ON artifact_caps(phys_key);
CREATE TABLE IF NOT EXISTS repositories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL,
  quota_bytes INTEGER NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repo_items (
  id TEXT PRIMARY KEY,
  repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  why TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('file','site')),
  added_by TEXT NOT NULL,
  source_artifact TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  latest INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS repo_items_repo ON repo_items(repo_id, updated_at);
CREATE TABLE IF NOT EXISTS repo_item_revisions (
  item_id TEXT NOT NULL REFERENCES repo_items(id) ON DELETE CASCADE,
  n INTEGER NOT NULL,
  created_at REAL NOT NULL,
  files INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  entry TEXT,
  manifest TEXT NOT NULL,
  added_by TEXT NOT NULL,
  source_artifact TEXT,
  source_rev INTEGER,
  PRIMARY KEY (item_id, n)
);
"""

#: One writer at a time in this process (the CLI and the unit run one uvicorn process).
_write_lock = threading.RLock()
_ready_for: str | None = None


def _touch_0600(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def reset_for_tests() -> None:
    global _ready_for
    _ready_for = None


@contextlib.contextmanager
def _con() -> Iterator[sqlite3.Connection]:
    """A connection to the store, its schema in place. Raises `ArtifactsUnavailable`."""
    global _ready_for
    path = db_path()
    if not path.exists():
        _ready_for = None  # a store removed while running is re-created with its schema
    try:
        _touch_0600(path)
        con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    except (OSError, sqlite3.Error) as e:
        raise ArtifactsUnavailable(str(e)) from None
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA secure_delete=ON")
        if _ready_for != str(path):
            with _write_lock:
                con.executescript(DDL)
                _add_relay_columns(con)
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _ready_for = str(path)
        yield con
    except sqlite3.Error as e:
        raise ArtifactsUnavailable(str(e)) from None
    finally:
        con.close()


def _add_relay_columns(con: sqlite3.Connection) -> None:
    """The relay-hosted public copy of a Home Free box's shared artifact (#2190): its relay id and
    URL. Added in place to a store that predates them."""
    have = {r[1] for r in con.execute("PRAGMA table_info(artifacts)")}
    for col, kind in (
        ("relay_public_id", "TEXT"),
        ("relay_url", "TEXT"),
        ("relay_rev", "INTEGER"),
        # A first upload to the relay may be in flight or may have landed unacknowledged: set
        # BEFORE the upload, cleared only on a definite outcome (#2190).
        ("relay_pending", "INTEGER"),
        # The box-hosted public link's options (#2217): a password hash, an expiry, the header.
        ("public_password_hash", "TEXT"),
        ("public_expires_at", "REAL"),
        ("public_header", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if col not in have:
            con.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {kind}")


def _absent() -> bool:
    """No store yet: a READ answers empty without creating one (a read leaves every store
    byte-identical, the HQ draft rule)."""
    return not db_path().exists()


@contextlib.contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """One write transaction under the process write lock."""
    with _write_lock, _con() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")


def new_id() -> str:
    return "-".join(str(1000 + secrets.randbelow(9000)) for _ in range(4))


def new_public_id() -> str:
    return secrets.token_urlsafe(16)  # 128 bits, 22 characters


def validate_id(value: object) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ArtifactRefused("unknown_artifact", status=404)
    return value


# ---------------------------------------------------------------- activation


def enabled() -> bool:
    """Settings → Artifacts: the global switch, ON unless explicitly turned off."""
    return prefs.get_artifacts_enabled()


def get_activation(scope: str, scope_id: str) -> str | None:
    """The mode stored for one scope, or None when nothing is set there (it inherits)."""
    if scope not in SCOPES or not isinstance(scope_id, str) or not scope_id:
        raise ValueError("bad scope")
    if _absent():
        return None
    with _con() as con:
        row = con.execute(
            "SELECT mode FROM artifact_activation WHERE scope=? AND scope_id=?", (scope, scope_id)
        ).fetchone()
    return row["mode"] if row else None


def set_activation(scope: str, scope_id: str, mode: str | None) -> str | None:
    """Set (or with ``None`` clear, so it inherits again) one scope's mode."""
    if scope not in SCOPES or not isinstance(scope_id, str) or not scope_id or len(scope_id) > 512:
        raise ValueError("bad scope")
    if mode is not None and mode not in MODES:
        raise ValueError("bad mode")
    with _tx() as con:
        if mode is None:
            con.execute(
                "DELETE FROM artifact_activation WHERE scope=? AND scope_id=?", (scope, scope_id)
            )
        else:
            con.execute(
                "INSERT INTO artifact_activation (scope, scope_id, mode, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(scope, scope_id) DO UPDATE SET"
                " mode=excluded.mode, updated_at=excluded.updated_at",
                (scope, scope_id, mode, time.time()),
            )
    return mode


@dataclass(frozen=True)
class Effective:
    """What one session's NEXT launch gets: the mode and where it came from."""

    mode: str  # off | private | public
    source: str  # global | session | mission | project | default | unreadable


def effective_mode(
    session_keys: list[str], *, cwd: str | None, mission_id: str | None = None
) -> Effective:
    """The most specific setting wins: the session's own, then its mission's, then its project's
    (by the project owning ``cwd``), else `DEFAULT_MODE` (private). With the global switch off,
    always off. Never raises. FAIL CLOSED: an unreadable store, missions store or projects index is
    off (source ``unreadable``): with a default of ON, a scope that cannot be read might be the one
    that says OFF, so it is never read as "nothing set" (Hermes on #1964)."""
    try:
        if not enabled():
            return Effective("off", "global")
        if _absent():
            return Effective(DEFAULT_MODE, "default")  # nothing set anywhere yet
        keys = [k for k in dict.fromkeys(session_keys) if isinstance(k, str) and k]
        with _con() as con:

            def mode_of(scope: str, sid: str) -> str | None:
                row = con.execute(
                    "SELECT mode FROM artifact_activation WHERE scope=? AND scope_id=?",
                    (scope, sid),
                ).fetchone()
                return row["mode"] if row else None

            for k in keys:
                m = mode_of("session", k)
                if m is not None:
                    return Effective(m, "session")
            mid = mission_id
            if mid is None:
                from . import missions

                for k in keys:
                    mid = missions.session_mission(
                        k
                    )  # unreadable: raises → off, never "no mission"
                    if mid:
                        break
            if mid:
                m = mode_of("mission", mid)
                if m is not None:
                    return Effective(m, "mission")
            pid = project_of(cwd, strict=True)  # unreadable: raises → off
            if pid:
                m = mode_of("project", pid)
                if m is not None:
                    return Effective(m, "project")
        return Effective(DEFAULT_MODE, "default")
    except Exception:  # noqa: BLE001 — activation that cannot be read activates nothing
        log.warning("artifacts: activation unreadable; treating as off", exc_info=True)
        return Effective("off", "unreadable")


def project_of(cwd: str | None, *, strict: bool = False) -> str | None:
    """The id of the project owning ``cwd``, or None. ``strict``: an unreadable projects index
    RAISES instead of answering "no project" (activation must fail closed on it)."""
    if not cwd:
        return None
    from . import projects

    try:
        index = projects.load(strict=strict)
        written = os.path.normpath(os.path.expanduser(cwd))
        # As written first (the form the projects index stores), then resolved.
        p = projects.owning_project(written, index) or projects.owning_project(
            os.path.realpath(written), index
        )
    except Exception:  # noqa: BLE001 — unreadable index: no project (or raise, when strict)
        if strict:
            raise
        return None
    if p is None or getattr(p, "archived", False):
        return None
    return p.id


# ---------------------------------------------------------------- the boundary


def session_boundary(cwd: str, session_key: str) -> mission_shelf.Boundary:
    """Where a session's files may be: its cwd, its project's folders and — when a mission holds
    the session now — the mission's folders; AND the roots, outside the exclusions."""
    folders: list[str] = []

    def add(folder: object) -> None:
        if isinstance(folder, str) and folder.strip():
            real = os.path.realpath(os.path.expanduser(folder))
            if real not in folders:
                folders.append(real)

    add(cwd)
    from . import projects

    with contextlib.suppress(Exception):
        p = projects.owning_project(os.path.realpath(os.path.expanduser(cwd)), projects.load())
        if p is not None and not getattr(p, "archived", False):
            for f in p.folders:
                add(f)
    with contextlib.suppress(Exception):
        from . import missions

        mid = missions.session_mission(session_key)
        if mid:
            for f in mission_shelf.mission_folders(missions.get_mission(mid)):
                add(f)
    return mission_shelf.Boundary(
        folders=tuple(folders),
        roots=tuple(project_dirs.effective_roots()),
        exclusions=mission_shelf._both(prefs.get_folder_exclusions()),
    )


# ---------------------------------------------------------------- publishing


@dataclass(frozen=True)
class Origin:
    """Who publishes: the identity the broker mapped from a launch's capability, never the agent's
    own claim."""

    engine: str
    session_key: str
    phys_key: str
    cwd: str


def _clean_line(value: object, cap: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArtifactRefused("bad_request")
    text = mission_shelf.clean_line(value, cap)
    if len(text) > cap:
        raise ArtifactRefused("too_long")
    return text


def _validate(args: object) -> dict:
    if not isinstance(args, dict):
        raise ArtifactRefused("bad_request")
    path = args.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > PATH_MAX or "\x00" in path:
        raise ArtifactRefused("bad_request")
    kind = args.get("kind")
    if kind is not None and kind not in KINDS:
        raise ArtifactRefused("bad_request")
    title = _clean_line(args.get("title"), TITLE_MAX, "title")
    if not title:
        raise ArtifactRefused("title_required")
    why = _clean_line(args.get("why"), WHY_MAX, "why")
    aid = args.get("artifact_id")
    if aid is not None:
        aid = validate_id(aid)
    repo = args.get("repository")
    if repo is not None:
        # A hosted repository's name or id (#2347 P1); `repo_store.lookup` decides which.
        if not isinstance(repo, str) or not repo.strip() or len(repo) > REPO_REF_MAX:
            raise ArtifactRefused("bad_request")
        repo = repo.strip()
    return {
        "path": path,
        "kind": kind,
        "title": title,
        "why": why,
        "artifact_id": aid,
        "repository": repo,
    }


def _rel_ok(rel: str) -> bool:
    parts = rel.split("/")
    return (
        len(rel) <= REL_PATH_MAX
        and all(p and p not in (".", "..") and len(p) <= SEGMENT_MAX for p in parts)
        and not mission_shelf._CONTROL.search(rel)
        and "\\" not in rel
    )


def _walk_site(top: str) -> tuple[list[tuple[str, str]], int]:
    """Every file under a site's directory as ``(relative path, absolute path)``, sorted, and the
    number of dot-entries left out. A symlink anywhere refuses the publish (never followed)."""
    out: list[tuple[str, str]] = []
    skipped = 0
    stack = [("", top)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            entries = sorted(os.scandir(abs_dir), key=lambda e: e.name)
        except FileNotFoundError:
            raise ArtifactRefused("not_found", rel_dir or None) from None
        except OSError:
            raise ArtifactRefused("not_a_file", rel_dir or None) from None
        for e in entries:
            rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
            if e.name.startswith("."):
                skipped += 1
                continue
            if e.is_symlink():
                raise ArtifactRefused("symlink", rel)
            if not _rel_ok(rel):
                raise ArtifactRefused("bad_name", rel)
            if e.is_dir(follow_symlinks=False):
                stack.append((rel, e.path))
            elif e.is_file(follow_symlinks=False):
                out.append((rel, e.path))
                if len(out) > REVISION_FILES_MAX:
                    raise ArtifactRefused("too_many_files")
            else:
                raise ArtifactRefused("not_a_file", rel)
    out.sort()
    return out, skipped


def _blob_path(sha: str) -> Path:
    return content_dir() / "blobs" / sha[:2] / sha


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def _stage(fd: int, rel: str, budget: list[int]) -> tuple[Path, str, int]:
    """Copy the verified descriptor's bytes into a PRIVATE staging file: ``(path, sha256, bytes
    read)``. ``budget`` is the revision's remaining allowance, decremented by what is read (the
    file may have grown since its stat). Nothing reaches ``blobs/`` here: a staged file becomes a
    blob only inside the commit (`_place`), under the write lock, so a blob on disk is always one
    a committed revision names or one being committed now — and garbage collection can remove an
    unnamed blob at once, with no grace and no race."""
    tmp = _private_dir(content_dir() / "tmp") / f"{secrets.token_hex(8)}.part"
    h = hashlib.sha256()
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        pos = 0
        while True:
            chunk = os.pread(fd, _CHUNK, pos)
            if not chunk:
                break
            pos += len(chunk)
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise ArtifactRefused("too_large", rel)
            h.update(chunk)
            os.write(out, chunk)
        os.fsync(out)
    except BaseException:
        os.close(out)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    os.close(out)
    return tmp, h.hexdigest(), pos


def _place(staged: dict[str, Path]) -> None:
    """Move staged files into ``blobs/`` (called inside the commit, under the write lock). An
    identical blob already there is kept; ``os.replace`` would be as good — content-addressed."""
    for sha, tmp in staged.items():
        dest = _blob_path(sha)
        _private_dir(dest.parent)
        if dest.exists():
            continue
        os.replace(tmp, dest)


def _judge_name(rel: str) -> None:
    """Refuse a hidden path, a key/keystore file and an archive by its name alone (every path)."""
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        raise ArtifactRefused("hidden", rel)
    name = parts[-1].lower()
    stem, ext = os.path.splitext(name)
    if ext in CREDENTIAL_EXTS or name in CREDENTIAL_NAMES or stem in CREDENTIAL_NAMES:
        raise ArtifactRefused("credential", rel)
    if name.endswith(ARCHIVE_SUFFIXES):
        # An archive hides its members from every rule here (a zipped project carries its `.env`
        # and its keys). Publish the folder instead; the site host serves it file by file.
        raise ArtifactRefused("archive", rel)


def _decode_text(head: bytes, raw: bytes) -> str | None:
    """The file as text, or None when it is not text: UTF-16 by its byte-order mark, otherwise
    UTF-8 (errors replaced) when its head has no NUL."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if not mission_shelf.text_ok(head):
        return None
    return raw.decode("utf-8", errors="replace")


def _judge_staged(tmp: Path, size: int, rel: str) -> None:
    """Judge the bytes that WILL be published (the staged copy, not the workspace file, which may
    change between a read and a copy). Every file whose head is text is judged, whatever its
    extension: a `.pdf` that is really a PEM is judged like any other text."""
    with open(tmp, "rb") as f:
        head = f.read(_TEXT_HEAD)
        if _decode_text(head, b"") is None:
            return
        if size > TEXT_CHECK_MAX:
            raise ArtifactRefused("text_too_large", rel)
        f.seek(0)
        text = _decode_text(head, f.read(size + 1))
    if text is not None and secret_shaped(text):
        raise ArtifactRefused("credential", rel)


#: Every table whose rows name blobs: an artifact's revisions and a repository item's (#2347 P1).
#: Accounting (`_held`, `TOTAL_BYTES_MAX`) and garbage collection read exactly these, so a blob is
#: counted once and kept while ANY of them names it.
_REVISION_TABLES: tuple[str, ...] = ("artifact_revisions", "repo_item_revisions")


def _manifests(con: sqlite3.Connection) -> Iterator[dict]:
    """Every committed revision's manifest: the artifacts' and the repositories' alike."""
    for table in _REVISION_TABLES:
        for r in con.execute(f"SELECT manifest FROM {table}"):  # noqa: S608 — a fixed table name
            yield json.loads(r["manifest"])


def _held(con: sqlite3.Connection) -> dict[str, int]:
    """Every blob a committed revision names (an artifact's or a repository item's), with its
    size."""
    seen: dict[str, int] = {}
    for manifest in _manifests(con):
        for meta in manifest.values():
            seen[meta["sha256"]] = meta["size"]
    return seen


def _stored_bytes(con: sqlite3.Connection) -> int:
    """Bytes the store holds, counted once per distinct blob, whether an artifact or a repository
    holds it."""
    return sum(_held(con).values())


def public_now(row: dict, now: float | None = None) -> bool:
    """Whether anyone but the operator can open this artifact NOW, in any form: a live box link,
    a relay copy, or a relay publication whose outcome is not known yet (`view`'s ``public``)."""
    return _live(row, now) or bool(row.get("relay_public_id")) or bool(row.get("relay_pending"))


def publish(origin: Origin, args: object, *, private_only: bool = False) -> dict:
    """Judge and copy one publish; the artifact row as the agent sees it. Raises
    `ArtifactRefused` (the agent hears its code and, when there is one, the path).

    ``private_only`` (an automation's publish, #2248 P2): a revision is added behind
    ``artifact_id`` only while that artifact is private (`public_now` is false), decided INSIDE
    the write transaction that appends it, the same one a share or a relay publication takes. One
    that is public by then gets nothing; the content becomes a NEW private artifact in that same
    transaction, and the answer says so (``started_new_private``)."""
    item = _validate(args)
    repository = None
    if item["repository"] is not None:
        from . import repo_store

        # Before anything is read or copied: an unknown repository costs nothing.
        repository = repo_store.lookup(item["repository"])["id"]
    b = session_boundary(origin.cwd, origin.session_key)
    raw = item["path"]
    path = raw if os.path.isabs(raw) else os.path.join(origin.cwd, raw)
    path = os.path.normpath(os.path.expanduser(path))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ArtifactRefused("not_found") from None
    except OSError:
        raise ArtifactRefused("not_a_file") from None
    if stat.S_ISLNK(st.st_mode):
        raise ArtifactRefused("symlink")
    is_dir = stat.S_ISDIR(st.st_mode)
    kind = item["kind"] or ("site" if is_dir else "file")
    if (kind == "site") != is_dir:
        raise ArtifactRefused("kind_mismatch")
    real = os.path.realpath(path)
    if not mission_shelf.path_allowed(real, b):
        raise ArtifactRefused("outside_project")
    if is_dir:
        members, skipped = _walk_site(real)
        if not members:
            raise ArtifactRefused("empty_site")
    else:
        members, skipped = [(os.path.basename(real), real)], 0
    # Hidden paths are judged against the session folder that holds the path, so a single file
    # (or a site) under `.aws/`, `.ssh/` or named `.env` is refused, not just skipped.
    holder = max((f for f in b.folders if project_dirs.path_within(real, f)), key=len, default=None)
    if holder is not None and real != holder:
        _judge_name(os.path.relpath(real, holder))
    for rel, _abs in members:
        _judge_name(rel)
    # The caps from the stat sizes BEFORE anything is copied: a full store or an oversized site
    # costs no disk (the copy re-checks what it actually reads).
    sizes = 0
    for _rel, abs_path in members:
        with contextlib.suppress(OSError):
            sizes += os.lstat(abs_path).st_size
    if sizes > REVISION_BYTES_MAX:
        raise ArtifactRefused("too_large")
    if not _absent():
        with _con() as con:
            if _stored_bytes(con) + sizes > TOTAL_BYTES_MAX:
                raise ArtifactRefused("store_full")
    return _publish_members(
        origin, item, kind, members, skipped, b, private_only=private_only, repository=repository
    )


def _publish_members(
    origin: Origin,
    item: dict,
    kind: str,
    members: list[tuple[str, str]],
    skipped: int,
    b: mission_shelf.Boundary,
    *,
    private_only: bool = False,
    repository: str | None = None,
) -> dict:
    manifest: dict[str, dict] = {}
    staged: dict[str, Path] = {}
    try:
        return _stage_and_commit(
            origin,
            item,
            kind,
            members,
            skipped,
            b,
            manifest,
            staged,
            private_only=private_only,
            repository=repository,
        )
    finally:
        for tmp in staged.values():  # placed ones were moved; the rest go now
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _stage_and_commit(
    origin: Origin,
    item: dict,
    kind: str,
    members: list[tuple[str, str]],
    skipped: int,
    b: mission_shelf.Boundary,
    manifest: dict[str, dict],
    staged: dict[str, Path],
    *,
    private_only: bool = False,
    repository: str | None = None,
) -> dict:
    budget = [REVISION_BYTES_MAX]
    for rel, abs_path in members:
        try:
            fd, fst, _verified = mission_shelf.open_verified(abs_path, b)
        except mission_shelf.FsError as e:
            code = mission_shelf._fs_refusal(e).code
            raise ArtifactRefused(code if code in REFUSALS else "not_a_file", rel) from None
        try:
            if fst.st_size > budget[0]:
                raise ArtifactRefused("too_large", rel)
            tmp, sha, size = _stage(fd, rel, budget)
        finally:
            os.close(fd)
        if sha in staged:
            os.unlink(tmp)
        else:
            staged[sha] = tmp
            _judge_staged(tmp, size, rel)
        ext = os.path.splitext(rel)[1].lower()
        manifest[rel] = {"sha256": sha, "size": size, "type": TYPES.get(ext)}
    if kind == "site":
        entry = "index.html" if "index.html" in manifest else None
    else:
        entry = members[0][0]
    total = sum(m["size"] for m in manifest.values())
    now = time.time()
    with _tx() as con:
        held = _held(con)
        fresh = {m["sha256"]: m["size"] for m in manifest.values() if m["sha256"] not in held}
        if sum(held.values()) + sum(fresh.values()) > TOTAL_BYTES_MAX:
            raise ArtifactRefused("store_full")
        aid = item["artifact_id"]
        started_new = False
        if aid is not None:
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if row is None:
                raise ArtifactRefused("unknown_artifact", status=404)
            if not owns(row, origin):
                raise ArtifactRefused("not_yours", status=403)
            if private_only and public_now(dict(row), now):
                # Shared since the caller chose it: never revise it; a new private one instead,
                # in THIS transaction (a share takes the same write lock, so it is wholly before
                # this read or wholly after this commit).
                aid, started_new = None, True
            elif row["kind"] != kind:
                raise ArtifactRefused("kind_mismatch")
        if aid is not None:
            n = row["latest"] + 1
            con.execute(
                "UPDATE artifacts SET title=?, why=?, latest=?, updated_at=? WHERE id=?",
                (item["title"], item["why"] or row["why"], n, now, aid),
            )
        else:
            aid, n = new_id(), 1
            con.execute(
                "INSERT INTO artifacts (id, title, why, kind, engine, session_key, phys_key, cwd,"
                " project_id, mission_id, created_at, updated_at, latest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    item["title"],
                    item["why"],
                    kind,
                    origin.engine,
                    origin.session_key,
                    origin.phys_key,
                    origin.cwd,
                    project_of(origin.cwd),
                    _mission_of(origin.session_key),
                    now,
                    now,
                    n,
                ),
            )
        con.execute(
            "INSERT INTO artifact_revisions (artifact_id, n, created_at, files, bytes, entry,"
            " manifest) VALUES (?,?,?,?,?,?,?)",
            (aid, n, now, len(manifest), total, entry, json.dumps(manifest, sort_keys=True)),
        )
        con.execute(
            "DELETE FROM artifact_revisions WHERE artifact_id=? AND n <= ?",
            (aid, n - REVISIONS_MAX),
        )
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        added = None
        if repository is not None:
            # Into a hosted repository (#2347 P1), in THIS transaction: an unknown repository
            # (deleted since the lookup) or one over its quota refuses the whole publish, and the
            # artifact revision above rolls back with it, so nothing is stored.
            from . import repo_store

            added = repo_store.add_in_tx(
                con,
                repository,
                source=(aid, n),
                title=row["title"],
                why=row["why"],
                kind=kind,
                manifest=manifest,
                entry=entry,
                added_by=origin.session_key,
                now=now,
            )
        # The bytes become blobs only now, under the write lock, as this revision commits.
        _place(staged)
    if n > REVISIONS_MAX or (added is not None and added["pruned"]):
        _collect_garbage()
    out = view(dict(row))
    out["skipped_hidden"] = skipped
    if started_new:
        out["started_new_private"] = True
    if added is not None:
        out["repository"] = added["placed"]
    return out


def owns(row, origin: Origin) -> bool:
    """Did this launch's session publish the artifact? By its logical key OR its physical key: an
    engine that names its session late (opencode, codex) relaunches under a new logical key while
    the physical key stays the same."""
    return row["session_key"] == origin.session_key or row["phys_key"] == origin.phys_key


def owned(aid: str, origin: Origin) -> bool | None:
    """Is the artifact this launch's session's (`owns`)? None when there is no such artifact."""
    if _absent():
        return None
    with _con() as con:
        row = con.execute(
            "SELECT session_key, phys_key FROM artifacts WHERE id=?", (aid,)
        ).fetchone()
    return None if row is None else owns(row, origin)


def _mission_of(session_key: str) -> str | None:
    from . import missions

    with contextlib.suppress(Exception):
        return missions.session_mission(session_key)
    return None


#: A STAGING file this old belongs to no publish in flight (a crash left it). Blobs need no grace:
#: they are placed only inside a commit, under the same lock collection takes.
GC_GRACE_S = 3600.0


def _collect_garbage(*, grace: float | None = None) -> None:
    """Remove every blob no revision names (at once), and staging files older than the grace. A
    repository item's revision (#2347 P1) names its blobs exactly as an artifact's does. Never
    raises."""
    grace = GC_GRACE_S if grace is None else grace
    try:
        with _write_lock, _con() as con:
            # Every revision that names a blob keeps it: an artifact's or a repository item's.
            live: set[str] = set()
            for manifest in _manifests(con):
                live.update(m["sha256"] for m in manifest.values())
            blobs = content_dir() / "blobs"
            if blobs.is_dir():
                for sub in blobs.iterdir():
                    for f in sub.iterdir():
                        if f.name not in live:
                            with contextlib.suppress(OSError):
                                f.unlink()
            tmp = content_dir() / "tmp"
            if tmp.is_dir():
                for f in tmp.iterdir():
                    if time.time() - f.stat().st_mtime >= grace:
                        with contextlib.suppress(OSError):
                            f.unlink()
    except Exception:  # noqa: BLE001 — garbage stays until the next pass
        log.warning("artifacts: garbage collection failed", exc_info=True)


# ---------------------------------------------------------------- reading


def base_url() -> str:
    """The app's own origin for links an agent or HQ hands out (``AGENT_SESSIONS_ORIGIN``)."""
    return (os.environ.get("AGENT_SESSIONS_ORIGIN") or "").rstrip("/")


def operator_url(aid: str) -> str:
    return f"{base_url()}/artifacts/{aid}/"


def public_url(public_id: str) -> str:
    return f"{base_url()}/artifacts/p/{public_id}/"


def view(row: dict) -> dict:
    """An artifact as the operator API, the agent's tool and HQ see it. The public URL is only
    present while the artifact is shared."""
    live = _live(row)
    out = {
        "id": row["id"],
        "title": row["title"],
        "why": row["why"],
        "kind": row["kind"],
        "revision": row["latest"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "origin": {
            "engine": row["engine"],
            "session_key": row["session_key"],
            "cwd": row["cwd"],
            "project_id": row["project_id"],
            "mission_id": row["mission_id"],
        },
        # A relay copy that may exist (an unacknowledged first upload) is never called private.
        # A box link past its expiry (#2217) is: it opens nothing.
        "visibility": (
            "public"
            if live or row.get("relay_public_id") or row.get("relay_pending")
            else "private"
        ),
        "url": operator_url(row["id"]),
        "public_url": (
            row["relay_url"]
            if row.get("relay_public_id")
            else public_url(row["public_id"])
            if live
            else None
        ),
        # Where the public copy lives: this box, or the BattleLab relay (Home Free, #2190).
        "hosted_on": "relay"
        if row.get("relay_public_id") or row.get("relay_pending")
        else "box"
        if live
        else None,
        # True while a first relay upload's outcome is unknown: revoke (or share again) settles it.
        "relay_uncertain": bool(row.get("relay_pending")) and not row.get("relay_public_id"),
        "public_since": row.get("public_since") if live else None,
        # The box link's options (#2217), never the hash. An expired link keeps its row so the
        # operator sees WHEN it ended; a re-share mints a new link.
        "public_has_password": bool(row.get("public_password_hash")) if live else False,
        "public_expires_at": row.get("public_expires_at") if row.get("public_id") else None,
        "public_expired": bool(row.get("public_id")) and not live,
        "public_header": bool(row.get("public_header", 1)) if live else True,
    }
    return out


def get(aid: str) -> dict | None:
    """One artifact with its revisions (newest first) and the latest revision's files."""
    validate_id(aid)
    if _absent():
        return None
    with _con() as con:
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None:
            return None
        revs = con.execute(
            "SELECT n, created_at, files, bytes, entry, manifest FROM artifact_revisions"
            " WHERE artifact_id=? ORDER BY n DESC",
            (aid,),
        ).fetchall()
    out = view(dict(row))
    out["revisions"] = [
        {"n": r["n"], "created_at": r["created_at"], "files": r["files"], "bytes": r["bytes"]}
        for r in revs
    ]
    if revs:
        latest = json.loads(revs[0]["manifest"])
        out["entry"] = revs[0]["entry"]
        out["files"] = [
            {"path": p, "size": m["size"], "type": m["type"]} for p, m in sorted(latest.items())
        ]
    return out


def list_artifacts(
    *,
    project: str | None = None,
    session: str | None = None,
    owner: tuple[str, ...] | None = None,
    mission: str | None = None,
    kind: str | None = None,
    visibility: str | None = None,
    q: str | None = None,
    since: float | None = None,
    cursor: str | None = None,
    limit: int = PAGE_MAX,
) -> dict:
    """A page of artifacts, newest first: ``{items, next_cursor, total}``."""
    where, params = ["1=1"], []
    if project:
        where.append("a.project_id=?")
        params.append(project)
    if session:
        where.append("(a.session_key=? OR a.phys_key=?)")
        params += [session, session]
    if owner:
        marks = ",".join("?" * len(owner))
        where.append(f"(a.session_key IN ({marks}) OR a.phys_key IN ({marks}))")
        params += [*owner, *owner]
    if mission:
        where.append("a.mission_id=?")
        params.append(mission)
    if kind:
        if kind not in KINDS:
            raise ValueError("bad kind")
        where.append("a.kind=?")
        params.append(kind)
    if visibility:
        if visibility not in VISIBILITIES:
            raise ValueError("bad visibility")
        # Public on the box OR as a relay-hosted copy (#2190); private is neither.
        # A box link past its expiry (#2217) is private: it opens nothing.
        public = (
            "((a.public_id IS NOT NULL"
            " AND (a.public_expires_at IS NULL OR a.public_expires_at > ?))"
            " OR a.relay_public_id IS NOT NULL OR COALESCE(a.relay_pending, 0) = 1)"
        )  # never NULL, so NOT (…) is exactly the rest
        where.append(public if visibility == "public" else f"NOT {public}")
        params.append(time.time())
    if q:
        needle = f"%{q.strip().lower()[:200]}%"
        where.append("(lower(a.title) LIKE ? OR lower(a.why) LIKE ? OR lower(a.cwd) LIKE ?)")
        params += [needle, needle, needle]
    if since is not None:
        where.append("a.updated_at >= ?")
        params.append(float(since))
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            raise ValueError("bad cursor") from None
    limit = max(1, min(PAGE_MAX, int(limit)))
    if _absent():
        return {"items": [], "total": 0, "next_cursor": None}
    sql_where = " AND ".join(where)
    with _con() as con:
        count_sql = f"SELECT count(*) FROM artifacts a WHERE {sql_where}"  # noqa: S608 — fixed clauses
        total = con.execute(count_sql, params).fetchone()[0]
        rows = con.execute(
            f"SELECT a.*, r.files, r.bytes, r.entry FROM artifacts a LEFT JOIN artifact_revisions r"  # noqa: S608
            f" ON r.artifact_id=a.id AND r.n=a.latest WHERE {sql_where}"
            " ORDER BY a.updated_at DESC, a.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        v = view(dict(r))
        v["files"] = r["files"]
        v["bytes"] = r["bytes"]
        # The file the stable link opens (null for a site without an index.html): the list's kind
        # tag (PNG, PDF…) and the session tab's image tile read it (#1932 P2).
        v["entry"] = r["entry"]
        items.append(v)
    nxt = offset + len(rows)
    return {"items": items, "total": total, "next_cursor": str(nxt) if nxt < total else None}


@dataclass(frozen=True)
class Served:
    """One file of one revision, ready to stream: its blob, size and type (None = download)."""

    blob: Path
    size: int
    type: str | None
    name: str


class NotServed(Exception):
    """No such artifact / revision / file (always 404, the same for every reason)."""


class Redirect(Exception):
    """A directory request: answer with the location (a site's index under a trailing slash)."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


def resolve(aid: str, rel: str, *, revision: int | None = None) -> Served:
    """The file ``rel`` names in the artifact's revision (latest by default), by MANIFEST LOOKUP.
    An empty ``rel`` is the entry point; a directory is its ``index.html``. Raises `NotServed`."""
    if not ID_RE.fullmatch(aid or "") or _absent():
        raise NotServed()
    with _con() as con:
        row = con.execute("SELECT latest FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise NotServed()
        n = row["latest"] if revision is None else revision
        rev = con.execute(
            "SELECT entry, manifest FROM artifact_revisions WHERE artifact_id=? AND n=?", (aid, n)
        ).fetchone()
    if rev is None:
        raise NotServed()
    return resolve_in(rev["entry"], json.loads(rev["manifest"]), rel)


def resolve_in(entry: str | None, manifest: dict, rel: str) -> Served:
    """The file ``rel`` names in ONE revision's manifest (an artifact's or a repository item's,
    #2347 P1), by lookup only. Raises `NotServed` or `Redirect`."""
    rel = rel or ""
    if rel == "":
        if entry is None:
            raise Redirect("index")  # a site without index.html: the generated listing
        rel = entry
    elif rel.endswith("/"):
        rel = rel + "index.html"
    meta = manifest.get(rel)
    if meta is None:
        if rel + "/index.html" in manifest:
            raise Redirect(rel.rsplit("/", 1)[-1] + "/")
        raise NotServed()
    blob = _blob_path(meta["sha256"])
    if not blob.is_file():
        raise NotServed()
    return Served(blob=blob, size=meta["size"], type=meta["type"], name=rel.rsplit("/", 1)[-1])


def listing(aid: str, *, revision: int | None = None) -> list[dict]:
    """The files of a revision (for a site without an index.html)."""
    with _con() as con:
        row = con.execute("SELECT latest FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise NotServed()
        n = row["latest"] if revision is None else revision
        rev = con.execute(
            "SELECT manifest FROM artifact_revisions WHERE artifact_id=? AND n=?", (aid, n)
        ).fetchone()
    if rev is None:
        raise NotServed()
    return [{"path": p, "size": m["size"]} for p, m in sorted(json.loads(rev["manifest"]).items())]


#: Public links are off in ``AUTH_MODE=none`` (every visitor would be the operator, as in
#: ``public-shares.md``). Set by `routes.artifacts.register` from the app's config.
PUBLIC_AVAILABLE = True
PUBLIC_OFF_REASON = (
    "Public links are off while BattleLab runs without a login (AUTH_MODE=none), which is also how"
    " Home Free stream mode runs."
)


#: A link's password is at most this long (`shares.MAX_PASSWORD`'s rule); a link lasts at most
#: `MAX_EXPIRY_S` (ten years, as session links).
PUBLIC_PASSWORD_MAX = 256
MAX_EXPIRY_S = 10 * 365 * 86400


def _live(row: dict, now: float | None = None) -> bool:
    """A public link that may be opened right now: minted and not expired."""
    if not row.get("public_id"):
        return False
    exp = row.get("public_expires_at")
    return exp is None or exp > (time.time() if now is None else now)


def hash_public_password(password: object) -> str | None:
    """Validate and hash a public link's password (None/empty = none). Slow (PBKDF2): the share
    route runs it before the write transaction. Raises `ValueError`."""
    if password is None or password == "":
        return None
    if not isinstance(password, str):
        raise ValueError("the password must be text")
    if len(password) > PUBLIC_PASSWORD_MAX:
        raise ValueError("the password is too long")
    check_password_text(password)
    from . import auth

    return auth.hash_password(password)


def check_password_text(password: str) -> None:
    """A password is hashed as UTF-8; a lone surrogate (valid escaped JSON) is not. Refused, never
    silently dropped (Hermes on PR #2220)."""
    try:
        password.encode()
    except UnicodeEncodeError:
        raise ValueError("the password has a character that is not valid text") from None


def check_expiry(expires_at: object, now: float | None = None) -> float | None:
    """A share's expiry: None (never) or epoch seconds in the future, at most `MAX_EXPIRY_S` away.
    Raises `ValueError`."""
    if expires_at is None:
        return None
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
        raise ValueError("the expiry must be a time in seconds")
    now = time.time() if now is None else now
    try:
        value = float(expires_at)
    except OverflowError:  # a JSON integer beyond float range: never a real time
        raise ValueError(
            "the expiry is too far away" if expires_at > 0 else "the expiry is in the past"
        ) from None
    if value != value or value <= now:  # NaN, or the past
        raise ValueError("the expiry is in the past")
    if value > now + MAX_EXPIRY_S:
        raise ValueError("the expiry is too far away")
    return value


def share(
    aid: str,
    *,
    password_hash: str | None = None,
    expires_at: float | None = None,
    header: bool = True,
) -> dict:
    """Mint the artifact's public link (a fresh 128-bit id); the artifact as the API sees it.

    The options (``password_hash`` from `hash_public_password`, ``expires_at`` from
    `check_expiry`, ``header``) are written ONLY when a link is minted. A live link keeps its link
    AND its options (the same answer twice), so the agent's option-less share can never reset a
    password the operator set; changing an option is revoke + share again. An expired link is
    replaced by a NEW id: the old one never comes back. Raises `ArtifactRefused`."""
    validate_id(aid)
    if not PUBLIC_AVAILABLE:
        raise ArtifactRefused("public_off", status=409)
    with _tx() as con:
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise ArtifactRefused("unknown_artifact", status=404)
        if not _live(dict(row)):
            con.execute(
                "UPDATE artifacts SET public_id=?, public_since=?, public_password_hash=?,"
                " public_expires_at=?, public_header=? WHERE id=?",
                (new_public_id(), time.time(), password_hash, expires_at, 1 if header else 0, aid),
            )
            row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return view(dict(row))


def revoke(aid: str) -> dict:
    """End the artifact's public link now: the next request to it is a 404. A later share mints a
    NEW link; the old one never comes back. The link's options (its password hash included) go
    with it."""
    validate_id(aid)
    with _tx() as con:
        cur = con.execute(
            "UPDATE artifacts SET public_id=NULL, public_since=NULL, public_password_hash=NULL,"
            " public_expires_at=NULL, public_header=1 WHERE id=?",
            (aid,),
        )
        if cur.rowcount == 0:
            raise ArtifactRefused("unknown_artifact", status=404)
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return view(dict(row))


def relay_parts(aid: str) -> dict:
    """What a relay copy of the artifact is made of (#2190): its kind, the latest revision's
    manifest, the box's own blob path for each sha256, and the relay id it already has (or None).
    Raises `ArtifactRefused` for an unknown artifact."""
    validate_id(aid)
    with _con() as con:
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise ArtifactRefused("unknown_artifact", status=404)
        rev = con.execute(
            "SELECT manifest FROM artifact_revisions WHERE artifact_id=? AND n=?",
            (aid, row["latest"]),
        ).fetchone()
    if rev is None:
        raise ArtifactRefused("unknown_artifact", status=404)
    manifest = json.loads(rev["manifest"])
    return {
        "kind": row["kind"],
        "manifest": manifest,
        "blobs": {m["sha256"]: str(_blob_path(m["sha256"])) for m in manifest.values()},
        "relay_public_id": row["relay_public_id"],
        # The revision the relay copy holds; behind `latest` after a refresh that failed.
        "relay_rev": row["relay_rev"],
        "relay_pending": bool(row["relay_pending"]),
        "latest": row["latest"],
    }


def mark_relay_pending(aid: str, pending: bool) -> None:
    """Record (before a first upload) or clear (on a definite outcome) that a relay copy of this
    artifact may exist without the box knowing its id (#2190). Durable: survives a restart."""
    validate_id(aid)
    with _tx() as con:
        cur = con.execute(
            "UPDATE artifacts SET relay_pending=? WHERE id=?", (1 if pending else None, aid)
        )
        if cur.rowcount == 0:
            raise ArtifactRefused("unknown_artifact", status=404)


def set_relay(
    aid: str, relay_public_id: str | None, relay_url: str | None, relay_rev: int | None = None
) -> dict:
    """Record (or, with None, clear) the artifact's relay-hosted copy; the artifact as the API
    sees it. Set only after the relay confirmed the upload, cleared only after it confirmed the
    delete."""
    validate_id(aid)
    with _tx() as con:
        cur = con.execute(
            "UPDATE artifacts SET relay_public_id=?, relay_url=?, relay_rev=?, relay_pending=NULL,"
            " public_since=CASE WHEN ? IS NULL THEN public_since ELSE COALESCE(public_since, ?) END"
            " WHERE id=?",
            (relay_public_id, relay_url, relay_rev, relay_public_id, time.time(), aid),
        )
        if cur.rowcount == 0:
            raise ArtifactRefused("unknown_artifact", status=404)
        if relay_public_id is None:
            con.execute(
                "UPDATE artifacts SET public_since=NULL WHERE id=? AND public_id IS NULL", (aid,)
            )
        row = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return view(dict(row))


@dataclass(frozen=True)
class PublicLink:
    """A live public link as the public routes need it. ``password_hash`` never leaves the server:
    the routes verify against it and answer nothing of it."""

    artifact_id: str
    title: str
    kind: str
    updated_at: float
    header: bool
    password_hash: str | None


def public_link(public_id: str) -> PublicLink | None:
    """The live link ``public_id`` names, re-read on every request; None when there is none, it
    was revoked, it has expired, or public links are off. All four are one answer."""
    if not PUBLIC_AVAILABLE or not PUBLIC_ID_RE.fullmatch(public_id or "") or _absent():
        return None
    with _con() as con:
        row = con.execute(
            "SELECT id, title, kind, updated_at, public_id, public_password_hash,"
            " public_expires_at, public_header FROM artifacts WHERE public_id=?",
            (public_id,),
        ).fetchone()
    if row is None or not _live(dict(row)):
        return None
    return PublicLink(
        artifact_id=row["id"],
        title=row["title"],
        kind=row["kind"],
        updated_at=row["updated_at"],
        header=bool(row["public_header"]),
        password_hash=row["public_password_hash"] or None,
    )


def by_public_id(public_id: str) -> str | None:
    """The artifact a live public link names, re-read on every request; None when there is none,
    it was revoked, it expired, or public links are off."""
    link = public_link(public_id)
    return link.artifact_id if link else None


def public_meta(public_id: str) -> dict | None:
    """What the public viewer page shows of a live public link (#2068): the title, the kind, when
    it last changed and whether the header is shown. Nothing else — no session, folder, why or
    files. None when there is no such live link (or public links are off). The CALLER withholds
    it from a password link until the password is given."""
    link = public_link(public_id)
    if link is None:
        return None
    return {
        "title": link.title,
        "kind": link.kind,
        "updated_at": link.updated_at,
        "header": link.header,
    }


def delete(aid: str) -> bool:
    """Delete an artifact, every revision and (once no other revision names them) its blobs."""
    validate_id(aid)
    with _tx() as con:
        cur = con.execute("DELETE FROM artifacts WHERE id=?", (aid,))
        gone = cur.rowcount > 0
    if gone:
        _collect_garbage()
    return gone


def usage() -> dict:
    """The store's totals and caps (Settings → Artifacts)."""
    if _absent():
        held = count = public = 0
    else:
        with _con() as con:
            held = _stored_bytes(con)
            count = con.execute("SELECT count(*) FROM artifacts").fetchone()[0]
            public = con.execute(
                "SELECT count(*) FROM artifacts WHERE (public_id IS NOT NULL"
                " AND (public_expires_at IS NULL OR public_expires_at > ?))"
                " OR relay_public_id IS NOT NULL OR relay_pending = 1",
                (time.time(),),
            ).fetchone()[0]
    return {
        "artifacts": count,
        "public": public,
        "bytes": held,
        "caps": {
            "revision_bytes": REVISION_BYTES_MAX,
            "revision_files": REVISION_FILES_MAX,
            "revisions": REVISIONS_MAX,
            "total_bytes": TOTAL_BYTES_MAX,
        },
    }
