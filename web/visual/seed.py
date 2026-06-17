#!/usr/bin/env python3
"""Seed a deterministic, throwaway fake HOME for the visual-review capture (#96, Phase 2).

Populates one project per engine so the sidebar renders every engine badge
(claude/opencode/codex/gemini/antigravity) + the new-session project picker has entries —
without ever touching the operator's real ~/.claude. Every state store the app reads lives under
the target HOME (so running `agent-sessions serve` with HOME=<this dir> is fully isolated):

  <home>/.claude/projects/<enc>/<uuid>.jsonl          (claude — scanner.py)
  <home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl    (codex  — engines.CodexProvider)
  <home>/.gemini/tmp/<slug>/chats/session-*.jsonl      (gemini — engines.GeminiProvider)
  <home>/.gemini/tmp/project-map.json
  <home>/.gemini/antigravity-cli/conversations/<uuid>.db   (antigravity — AntigravityProvider)
  <home>/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript.jsonl
  <home>/.local/share/opencode/opencode.db             (opencode — engines.OpenCodeProvider)

Usage:  python web/visual/seed.py <home-dir>
Refuses to run if <home-dir> is (or contains) the real ~/.claude.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

# Deterministic ids so reruns + screenshots are stable.
_CLAUDE = [
    ("019e2ba1-1590-7003-8e4a-51ab62cec900", "/seed/alpha", "Investigate the failing build"),
    ("019e2ba1-1590-7003-8e4a-51ab62cec901", "/seed/beta", "Refactor the auth module"),
]
_CODEX = ("019e2ba1-1590-7003-8e4a-51ab62cec902", "/seed/alpha", "Wire up the deploy step")
_GEMINI = ("96fb77fc-9c1a-4453-b27b-d78d8012dd2c", "/seed/beta", "Tag the open issues")
_OPENCODE = ("ses_seed00000001", "/seed/alpha", "Port the scanner")
_ANTIGRAVITY = ("019e2ba1-1590-7003-8e4a-51ab62cec903", "/seed/gamma", "Port the deploy script")


def _refuse_real_home(home: Path) -> None:
    real = Path.home().resolve()
    h = home.resolve()
    if h == real or real.is_relative_to(h):
        raise SystemExit(f"refusing to seed over the real home {real} (target {h})")
    if (h / ".claude" / "projects").exists() and h == real:
        raise SystemExit("refusing: target already holds a real ~/.claude/projects")


def _real_cwd(home: Path, logical: str) -> str:
    """Map a logical seed cwd (e.g. ``/seed/alpha``) to a REAL directory under the throwaway
    home and create it, so the engine launcher can actually ``cd`` + start the (fake) agent
    there — otherwise resuming the seeded session fails with "couldn't start this session"
    (the cwd didn't exist) and the ``session-view`` capture shows an empty terminal (#211)."""
    name = logical.lstrip("/").replace("/", "-")  # /seed/alpha → seed-alpha
    p = home / "seed" / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def seed_claude(home: Path) -> None:
    for uuid, cwd, msg in _CLAUDE:
        cwd = _real_cwd(home, cwd)
        enc = "-" + cwd.lstrip("/").replace("/", "-")  # claude's dir encoding
        d = home / ".claude" / "projects" / enc
        d.mkdir(parents=True, exist_ok=True)
        rec = {"type": "user", "cwd": cwd, "message": {"role": "user", "content": msg}}
        (d / f"{uuid}.jsonl").write_text(json.dumps(rec) + "\n")


def seed_codex(home: Path) -> None:
    uuid, cwd, msg = _CODEX
    cwd = _real_cwd(home, cwd)
    d = home / ".codex" / "sessions" / "2026" / "05" / "15"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"timestamp": "t", "type": "session_meta", "payload": {"id": uuid, "cwd": cwd}},
        {
            "timestamp": "t",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": msg}],
            },
        },
    ]
    (d / f"rollout-2026-05-15T10-00-00-{uuid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n"
    )


def seed_gemini(home: Path) -> None:
    sid, cwd, msg = _GEMINI
    cwd = _real_cwd(home, cwd)
    phash = hashlib.sha256(cwd.encode()).hexdigest()
    tmp = home / ".gemini" / "tmp"
    chats = tmp / "seed-beta" / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    (tmp / "project-map.json").write_text(json.dumps({phash: cwd}))
    lines = [
        {
            "sessionId": sid,
            "projectHash": phash,
            "startTime": "t",
            "lastUpdated": "t",
            "kind": "main",
        },
        {"id": "x", "timestamp": "t", "type": "user", "content": [{"text": msg}]},
    ]
    (chats / f"session-2026-05-15T06-24-{sid[:8]}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n"
    )


def _varint(n: int) -> bytes:
    """protobuf little-endian base-128 varint — how agy length-prefixes the file:// workspace."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def seed_antigravity(home: Path) -> None:
    uuid, cwd, msg = _ANTIGRAVITY
    cwd = _real_cwd(home, cwd)
    base = home / ".gemini" / "antigravity-cli"
    (base / "conversations").mkdir(parents=True, exist_ok=True)
    (base / "cache").mkdir(parents=True, exist_ok=True)
    # The conversation db: AntigravityProvider reads cwd from the trajectory_metadata_blob 'main'
    # row, where agy stores the workspace as a varint-length-delimited file:// URI.
    uri = f"file://{cwd}".encode()
    blob = b"\n\x26\n" + _varint(len(uri)) + uri + b"z\xe8\x07"
    con = sqlite3.connect(base / "conversations" / f"{uuid}.db")
    try:
        con.execute(
            "CREATE TABLE trajectory_metadata_blob "
            "(id TEXT DEFAULT 'main', data BLOB, PRIMARY KEY(id))"
        )
        con.execute("INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)", (blob,))
        con.commit()
    finally:
        con.close()
    # cache/last_conversations.json (cwd→uuid) is the provider's robust cwd fast-path.
    (base / "cache" / "last_conversations.json").write_text(json.dumps({cwd: uuid}))
    logs = base / "brain" / uuid / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    recs = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": f"<USER_REQUEST>\n{msg}\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "On it.",
        },
    ]
    (logs / "transcript.jsonl").write_text("\n".join(json.dumps(x) for x in recs) + "\n")


def seed_opencode(home: Path) -> None:
    sid, directory, title = _OPENCODE
    directory = _real_cwd(home, directory)
    db_dir = home / ".local" / "share" / "opencode"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_dir / "opencode.db")
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS session "
            "(id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
            "time_updated INTEGER, time_archived INTEGER)"
        )
        con.execute(
            "INSERT INTO session (id, parent_id, directory, title, time_updated, time_archived) "
            "VALUES (?, NULL, ?, ?, ?, NULL)",
            (sid, directory, title, int(time.time() * 1000)),
        )
        con.commit()
    finally:
        con.close()


# Keep in sync with pulse.CACHE_VERSION (a mismatch makes load_cache treat this as a miss and
# /pulse renders the empty state — the populated `pulse` capture would silently go blank).
_PULSE_CACHE_VERSION = 1


def seed_pulse(home: Path) -> None:
    """Write a representative cached Pulse overview (#441 Phase 5) so the `/pulse` capture renders
    POPULATED: a depth-medium banner + cards across every state bucket (needs-you ⚠ / in-flight /
    recently-active / idle). The page serves this cache verbatim (GET never scans), so the
    screenshot is deterministic without driving a live scan."""
    now = int(time.time())

    def _folder(cwd: str, name: str) -> dict:
        return {"kind": "folder", "id": cwd, "name": name}

    def _card(sid, engine, title, cwd, proj, summary, state, *, age_s, live=False, reason=""):
        return {
            "id": f"{engine}:{sid}",
            "engine": engine,
            "title": title,
            "cwd": cwd,
            "project": proj,
            "last_activity": now - age_s,
            "ai_summary": summary,
            "intervention_required": bool(reason),
            "intervention_reason": reason,
            "reviewed_at": now - age_s,
            "live": live,
            "state": state,
            "synthesis": None,
        }

    cards = [
        _card(
            "019e2ba1-1590-7003-8e4a-51ab62cec902", "codex", "Wire up the deploy step",
            "/seed/alpha", _folder("/seed/alpha", "alpha"),
            "Deploy step is ready — waiting on your go-ahead to push.", "needs_you",
            age_s=240, reason="Confirm before it pushes to production",
        ),
        _card(
            "019e2ba1-1590-7003-8e4a-51ab62cec900", "claude", "Investigate the failing build",
            "/seed/alpha", _folder("/seed/alpha", "alpha"),
            "Bisecting the CI failure — narrowed to the last three commits.", "in_flight",
            age_s=30, live=True,
        ),
        _card(
            "019e2ba1-1590-7003-8e4a-51ab62cec901", "claude", "Refactor the auth module",
            "/seed/beta", _folder("/seed/beta", "beta"),
            "Split the token logic into its own module; tests green.", "recently_active",
            age_s=5400,
        ),
        _card(
            "ses_seed00000001", "opencode", "Port the scanner",
            "/seed/alpha", _folder("/seed/alpha", "alpha"),
            "Porting the directory scanner to the new engine API.", "recently_active",
            age_s=9000,
        ),
        _card(
            "96fb77fc-9c1a-4453-b27b-d78d8012dd2c", "gemini", "Tag the open issues",
            "/seed/beta", _folder("/seed/beta", "beta"),
            "Triaged 12 issues and applied area labels.", "idle",
            age_s=2 * 86400,
        ),
    ]
    artifact = {
        "cache_version": _PULSE_CACHE_VERSION,
        "generated_at": now - 90,
        "window_days": 3,
        "scan_depth": "medium",
        "input_fingerprint": "seed-fingerprint",
        "synthesis_skipped": False,
        "banner": (
            "1 session needs you — the deploy step is waiting on your go-ahead. The build "
            "investigation is in flight; auth refactor and scanner port are recently active."
        ),
        "cards": cards,
    }
    d = home / ".config" / "agent-sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pulse-cache.json").write_text(json.dumps(artifact, indent=2, sort_keys=True))


def seed(home: Path) -> None:
    _refuse_real_home(home)
    home.mkdir(parents=True, exist_ok=True)
    seed_claude(home)
    seed_codex(home)
    seed_gemini(home)
    seed_antigravity(home)
    seed_opencode(home)
    seed_pulse(home)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: seed.py <home-dir>")
    target = Path(sys.argv[1])
    seed(target)
    print(
        f"seeded {target}: claude(2) + codex(1) + gemini(1) + antigravity(1) + opencode(1) "
        "+ pulse-cache"
    )
