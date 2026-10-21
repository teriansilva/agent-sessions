/** Per-session file-panel state (#783), device-local and **bounded**.
 *
 * Switching sessions and coming back should not lose your place, which means remembering the
 * root and the expanded set per session id. That is exactly the shape that grows without limit
 * in a long-lived browser, so the store is an LRU with a hard entry cap: oldest-touched entries
 * are evicted, and the expanded set per session is capped too (a deep tree can otherwise
 * accumulate thousands of paths that no longer exist).
 */
export const STATE_KEY = "tr-filepanel-state";
export const MAX_SESSIONS = 20;
export const MAX_EXPANDED = 200;

export interface PanelSessionState {
  open: boolean;
  root: string | null;
  expanded: string[];
  /** Monotonic touch counter for LRU eviction (not a wall clock — no Date dependency). */
  seq: number;
}

interface Store {
  seq: number;
  sessions: Record<string, PanelSessionState>;
}

const EMPTY: Store = { seq: 0, sessions: {} };

function read(): Store {
  try {
    const raw = localStorage.getItem(STATE_KEY);
    if (!raw) return { seq: 0, sessions: {} };
    const parsed = JSON.parse(raw) as Store;
    if (!parsed || typeof parsed !== "object" || typeof parsed.sessions !== "object") return { seq: 0, sessions: {} };
    return { seq: Number(parsed.seq) || 0, sessions: parsed.sessions ?? {} };
  } catch {
    return { seq: 0, sessions: {} };
  }
}

function write(store: Store): void {
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify(store));
  } catch {
    /* quota / private mode — panel state is a nicety, never a hard failure */
  }
}

export function loadPanelState(sessionKey: string): PanelSessionState | null {
  const store = read();
  return store.sessions[sessionKey] ?? null;
}

export function savePanelState(
  sessionKey: string,
  next: Omit<PanelSessionState, "seq">,
): void {
  const store = read();
  const seq = store.seq + 1;
  store.seq = seq;
  store.sessions[sessionKey] = {
    open: next.open,
    root: next.root,
    // Cap the expanded set: keep the most recently added (the tail), which is the part the
    // user is actually looking at.
    expanded: next.expanded.slice(-MAX_EXPANDED),
    seq,
  };
  const keys = Object.keys(store.sessions);
  if (keys.length > MAX_SESSIONS) {
    keys
      .sort((a, b) => (store.sessions[a]?.seq ?? 0) - (store.sessions[b]?.seq ?? 0))
      .slice(0, keys.length - MAX_SESSIONS)
      .forEach((k) => delete store.sessions[k]);
  }
  write(store);
}

/** Move an entry from one session key to another, once, without clobbering an existing target.
 *
 *  opencode mints its own id (#127), so a fresh session lives under a client placeholder until
 *  the server reconciles it. Panel state written during that window is keyed to the placeholder,
 *  and a later reload — which starts at the REAL id — would find nothing. */
export function migratePanelState(from: string, to: string): void {
  if (from === to) return;
  const store = read();
  const src = store.sessions[from];
  if (!src || store.sessions[to]) return;
  store.seq += 1;
  store.sessions[to] = { ...src, seq: store.seq };
  delete store.sessions[from];
  write(store);
}

export function clearPanelState(): void {
  write(EMPTY);
}
