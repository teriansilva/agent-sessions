// Recoverable history of compose submissions (#619).
//
// Compose can only observe "the WS frame reached an OPEN socket" — never that the agent actually
// consumed the bytes. When an agent swallows a send (#616: a fresh claude clearing its screen over
// the paste), the composer and the server-side draft (#477) have already been cleared and the text
// is gone for good. This is the safety net: every submission is recorded BEFORE delivery, so a
// swallowed message stays recoverable.
//
// Device-global, not per-session. The obvious design is one ring per session key — but Terminal
// passes `sessionId={null}` for a freshly launched session (its id is still the `new-…`
// placeholder), which is exactly where the reported loss happened. A per-session ring would have
// missed the very message that motivated this. One ring, newest first, tagged with the session it
// came from.
//
// Fail-soft everywhere: private mode, a disabled/full localStorage, or corrupt JSON must never
// block a send or leave the composer half-cleared. A missing safety net is bad; a safety net that
// breaks the thing it protects is worse.

export interface SentMessage {
  id: string;
  /** The composed text, exactly as typed (untrimmed) so Restore round-trips it. */
  text: string;
  /** Server-side attachment paths — never blobs. */
  attachments: string[];
  ts: number;
  /** The socket accepted every frame. NEVER a claim that the agent processed it (#619). */
  confirmed: boolean;
  /** Engine-qualified session id, or null for a not-yet-reconciled fresh launch. */
  session: string | null;
}

export const SENT_HISTORY_KEY = "as:sent:v1";
/** The operator asked for "the last 10". */
export const MAX_ENTRIES = 10;
/** Total serialized budget, so one giant paste can't exhaust the localStorage quota. */
export const MAX_BYTES = 256 * 1024;

function store(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // disabled by policy / sandboxed iframe
  }
}

function isEntry(v: unknown): v is SentMessage {
  if (!v || typeof v !== "object") return false;
  const e = v as Record<string, unknown>;
  return (
    typeof e.id === "string" &&
    typeof e.text === "string" &&
    Array.isArray(e.attachments) &&
    e.attachments.every((a) => typeof a === "string") &&
    typeof e.ts === "number" &&
    typeof e.confirmed === "boolean" &&
    (e.session === null || typeof e.session === "string")
  );
}

/** The ring, newest first. `[]` on any storage/parse failure — a corrupt ring is not an error. */
export function readSent(): SentMessage[] {
  const s = store();
  if (!s) return [];
  try {
    const raw = s.getItem(SENT_HISTORY_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isEntry) : [];
  } catch {
    return [];
  }
}

/** Trim to MAX_ENTRIES, then drop oldest until the serialized ring fits MAX_BYTES. The newest entry
 *  is always kept — it is the one the operator is most likely to need back. */
function fit(entries: SentMessage[]): SentMessage[] {
  const out = entries.slice(0, MAX_ENTRIES);
  while (out.length > 1 && JSON.stringify(out).length > MAX_BYTES) out.pop();
  return out;
}

function write(entries: SentMessage[]): boolean {
  const s = store();
  if (!s) return false;
  try {
    s.setItem(SENT_HISTORY_KEY, JSON.stringify(entries));
    return true;
  } catch {
    return false; // quota exceeded / private mode — the send must not care
  }
}

function newId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

/** Record a submission at submit time, before the composer and the draft are cleared.
 *  Returns its id (for a later `confirmSent`), or null when nothing could be persisted. */
export function appendSent(entry: {
  text: string;
  attachments: string[];
  session: string | null;
}): string | null {
  const id = newId();
  const next = fit([
    { id, ts: Date.now(), confirmed: false, ...entry },
    ...readSent(),
  ]);
  return write(next) ? id : null;
}

/** Mark a recorded submission as delivered to the socket. A no-op if it has been evicted. */
export function confirmSent(id: string): void {
  const entries = readSent();
  const hit = entries.find((e) => e.id === id);
  if (!hit || hit.confirmed) return;
  hit.confirmed = true;
  write(entries);
}

/** Drop the whole ring — sent prompt text must not outlive the session on a shared device. */
export function clearSent(): void {
  const s = store();
  if (!s) return;
  try {
    s.removeItem(SENT_HISTORY_KEY);
  } catch {
    /* nothing we can do, and nothing that should break sign-out */
  }
}
