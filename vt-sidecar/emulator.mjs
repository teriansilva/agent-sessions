// Per-session headless-xterm emulator pool for Path B (#271/#273).
//
// LIVE MIRROR model (#273, the rebuild-from-ring replacement): one persistent @xterm/headless
// Terminal per session, fed the agent's PTY output INCREMENTALLY as it streams and resized in step
// with the agent's pty, so it tracks the real terminal exactly — Ink's frequent cursor-up repaints
// overwrite IN PLACE (no duplication) just as on the live console, and only genuinely-scrolled lines
// land in scrollback. On attach we snapshot: resize a view to the client width, read the reflowed
// rows, then restore to the agent geometry — non-destructive to the live feed or any other viewer.
//
// This supersedes the old one-shot rebuild(ring): replaying the whole saved ring at a single fixed
// geometry could not be faithful, because the ring is a layered soup of repaints authored at the
// different sizes the session was rendered at over its life → it duplicated. rebuild() is kept only
// as a cold-start fallback / for tests.
import headless from "@xterm/headless";
import { snapshotRows } from "./snapshot.mjs";

const { Terminal } = headless;

export const LIMITS = {
  // Authoring-width FLOOR for the legacy one-shot rebuild() path. MUST be >= the agent's pty width
  // or wide-authored lines clamp → right-side garble (#273). The LIVE mirror ignores this — it is
  // created at the agent's real geometry via open()/resize().
  feedCols: 512,
  defaultCols: 80, // geometry for a feed that arrives before any open()/resize() (should not happen)
  defaultRows: 24,
  scrollback: 5000, // max retained scrollback rows
  maxSessions: 64, // LRU cap across sessions
  maxSnapshotBytes: 2 * 1024 * 1024, // hard ceiling on a snapshot payload
};

export class EmulatorPool {
  constructor(limits = LIMITS) {
    this.limits = limits;
    this.sessions = new Map(); // key -> { term, touched }
  }

  _evictIfNeeded() {
    while (this.sessions.size > this.limits.maxSessions) {
      // drop the least-recently-touched
      let oldest = null;
      let oldestT = Infinity;
      for (const [k, s] of this.sessions) if (s.touched < oldestT) ((oldestT = s.touched), (oldest = k));
      if (oldest == null) break;
      this.end(oldest);
    }
  }

  _get(key, create, cols, rows) {
    let s = this.sessions.get(key);
    if (!s && create) {
      s = this._make(key, cols || this.limits.defaultCols, rows || this.limits.defaultRows);
    }
    if (s) s.touched = this._tick();
    return s;
  }

  // Create a fresh emulator for `key` at `cols`x`rows` — the agent's pty geometry. Tracked per
  // session (s.cols/s.rows) so the live feed renders at the agent's size and snapshot() restores to
  // it after reflowing a view to the client width.
  _make(key, cols, rows) {
    const term = new Terminal({
      cols: Math.max(2, cols | 0),
      rows: Math.max(2, rows | 0),
      scrollback: this.limits.scrollback,
      allowProposedApi: true,
    });
    const s = { term, touched: this._tick(), cols: term.cols, rows: term.rows };
    this.sessions.set(key, s);
    this._evictIfNeeded();
    return s;
  }

  // Live mirror: ensure an emulator exists for `key` and is sized to the agent's pty geometry. Called
  // on attach (with the agent's launch size) and on every agent resize, BEFORE feed()s at that size.
  // Creating-or-resizing here (not in feed) guarantees incoming bytes render at the correct width so
  // the agent's cursor-up repaints overwrite in place instead of duplicating.
  open(key, cols, rows) {
    const c = Math.max(2, cols | 0);
    const r = Math.max(2, rows | 0);
    const s = this._get(key, true, c, r);
    if (s.cols !== c || s.rows !== r) {
      s.term.resize(c, r); // xterm reflows the live buffer to the agent's new size
      s.cols = c;
      s.rows = r;
    }
    return s;
  }

  _tick() {
    // monotonic counter (Date.now may be coarse / non-monotonic under load)
    this._n = (this._n || 0) + 1;
    return this._n;
  }

  // Append live PTY bytes to the mirror. The emulator MUST already exist (open() on attach). A feed
  // for an unknown session — one never opened, or whose emulator was evicted/lost to a restart — is
  // REJECTED (returns false), never auto-created at a default geometry: a stray feed would otherwise
  // build a wrong-geometry emulator that later looks like faithful scrollback (Hermes #273). The
  // Python client marks such a key dirty so its snapshots fail safe to the transcript until reopened.
  feed(key, bytes) {
    const s = this._get(key, false);
    if (!s) return Promise.resolve(false);
    return new Promise((resolve) => s.term.write(bytes, () => resolve(true)));
  }

  // Reflow a VIEW to the client (cols,rows), read the styled rows incl. scrollback, then restore to
  // the agent geometry — non-destructive to the live feed. Returns "" if the session is unknown.
  async snapshot(key, cols, rows) {
    const s = this._get(key, false);
    if (!s) return "";
    s.term.resize(Math.max(2, cols | 0), Math.max(2, rows | 0));
    let payload = snapshotRows(s.term);
    s.term.resize(s.cols, s.rows); // restore to the agent geometry — non-destructive
    if (Buffer.byteLength(payload) > this.limits.maxSnapshotBytes) {
      // keep the TAIL (most recent) within the cap
      const buf = Buffer.from(payload);
      payload = buf.subarray(buf.length - this.limits.maxSnapshotBytes).toString("utf8");
    }
    return payload;
  }

  reset(key) {
    const s = this.sessions.get(key);
    if (s) s.term.reset();
  }

  // Rebuild a session's emulator from scratch (the rebuild-from-ring attach path, #273): drop any
  // existing emulator, feed the raw bytes at the authoring width, and return the snapshot at the
  // client width — all in one call so concurrent attaches on one key can't interleave reset/feed.
  async rebuild(key, bytes, cols, rows) {
    this.end(key);
    // Feed at >= the client width so a wide-authored ring (e.g. a 300-col desktop session) isn't
    // clamped at the floor and garbled. snapshot then reflows DOWN to the actual client width.
    const feedW = Math.max(this.limits.feedCols, cols | 0);
    const s = this._make(key, feedW, Math.max(2, rows | 0));
    await new Promise((resolve) => s.term.write(bytes, resolve));
    return this.snapshot(key, cols, rows);
  }

  end(key) {
    const s = this.sessions.get(key);
    if (s) {
      try {
        s.term.dispose();
      } catch {
        /* best effort */
      }
      this.sessions.delete(key);
    }
  }

  stats() {
    return { sessions: this.sessions.size, ...this.limits };
  }
}
