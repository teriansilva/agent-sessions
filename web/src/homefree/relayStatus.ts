/** The relay connection, shared by the connect shell and the streamed app (#1658).
 *
 *  The connect page (connect.main.ts) dynamically imports the SPA inside ONE Vite module graph
 *  (appMount.ts), so both sides hold this same module instance: the shell publishes what the
 *  connection looks like, and the app's top bar (`RelayChip`) shows it as a plate. No `window`
 *  global, no event protocol. In the normal app nothing ever publishes, the snapshot stays `null`
 *  and the plate renders nothing.
 *
 *  **Display fields and one callback, nothing else.** The access password, the session keys and
 *  the socket stay in the shell; `signOut` is the shell's own Sign out path.
 *
 *  **Attempt-scoped.** The shell publishes once its attempt is connected and clears on every exit
 *  (sign out, close, expiry, a newer attempt). An update or a sign out reaching a cleared store is
 *  a no-op, so a late event of an ended attempt cannot paint a plate or end a newer session.
 *
 *  **The dock count** is how the shell knows a plate is actually showing: a mounted plate docks,
 *  an unmounted one undocks, and the shell keeps its own floating bar only while nothing is docked
 *  — so Sign out is reachable even when the app has no top bar. */

export type RelayLink = "linked" | "ending";

export interface RelaySnapshot {
  link: RelayLink;
  /** The console key the operator connected to. */
  name: string;
  /** The shell's status line ("connected — streaming …", "session ends soon …"). */
  status: string;
  /** Time left, as the shell's countdown reads it ("3:57:50", "04:59"); empty when unknown. */
  remaining: string;
}

/** The shell's countdown ("3:57:50", "04:59") to the minute: "3 h 57 min left", "4 min left". */
export function minutesLeft(remaining: string): string {
  const parts = remaining.split(":").map(Number);
  if (parts.length < 2 || parts.some((n) => !Number.isFinite(n))) return "";
  const [h, m] = parts.length === 3 ? [parts[0], parts[1]] : [0, parts[0]];
  if (h === 0 && m === 0) return "under a minute left";
  return h > 0 ? `${h} h ${m} min left` : `${m} min left`;
}

let snapshot: RelaySnapshot | null = null;
let signOutFn: (() => void) | null = null;
let docked = 0;
const listeners = new Set<() => void>();
const dockListeners = new Set<(docked: boolean) => void>();

function emit(): void {
  for (const fn of listeners) fn();
}

/** The shell, once its attempt is connected: what to show, and how to sign out. */
export function publishRelay(next: RelaySnapshot, signOut: () => void): void {
  snapshot = next;
  signOutFn = signOut;
  emit();
}

/** A change to the published connection. Nothing is published → nothing to change. */
export function updateRelay(patch: Partial<RelaySnapshot>): void {
  if (!snapshot) return;
  const next = { ...snapshot, ...patch };
  if (
    next.link === snapshot.link &&
    next.name === snapshot.name &&
    next.status === snapshot.status &&
    next.remaining === snapshot.remaining
  )
    return;
  snapshot = next;
  emit();
}

/** The attempt ended: no connection to show, and nothing a stale plate could sign out of. */
export function clearRelay(): void {
  if (!snapshot && !signOutFn) return;
  snapshot = null;
  signOutFn = null;
  emit();
}

export function getRelaySnapshot(): RelaySnapshot | null {
  return snapshot;
}

export function subscribeRelay(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The plate's Sign out: the shell's own path, or nothing once the attempt has ended. */
export function relaySignOut(): void {
  signOutFn?.();
}

/** A plate is showing. Returns its undock, which counts once however often it is called. */
export function dockRelayPlate(): () => void {
  docked += 1;
  if (docked === 1) for (const fn of dockListeners) fn(true);
  let done = false;
  return () => {
    if (done) return;
    done = true;
    docked -= 1;
    if (docked === 0) for (const fn of dockListeners) fn(false);
  };
}

export function relayPlateDocked(): boolean {
  return docked > 0;
}

/** The shell's side of the dock: told when the first plate docks and when the last one leaves. */
export function onRelayDockChange(fn: (docked: boolean) => void): () => void {
  dockListeners.add(fn);
  return () => {
    dockListeners.delete(fn);
  };
}
