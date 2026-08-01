// Builds the /ws/term URL for a session. Pure (reads only location) so the new-session
// param wiring is unit-testable. A fresh session adds ?new=1&cwd=&bypass=; the server
// only acts on those when it actually launches — once a master exists it ATTACHes and
// ignores them, so it's safe to keep sending them across reconnects.

export interface FreshSession {
  /** A pickable project cwd to launch the new session in. */
  cwd: string;
  /** Permission-bypass choice (claude --dangerously-skip-permissions); default on. */
  bypass: boolean;
}

export function termWsUrl(
  engine: string,
  id: string,
  have: number,
  fresh?: FreshSession,
  opts?: {
    fp?: string;
    tabId?: string;
    force?: boolean;
    cols?: number;
    rows?: number;
    label?: string;
  },
): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const key = `${encodeURIComponent(engine)}:${encodeURIComponent(id)}`;
  const params = new URLSearchParams({ have: String(have) });
  if (fresh) {
    params.set("new", "1");
    params.set("cwd", fresh.cwd);
    params.set("bypass", fresh.bypass ? "1" : "0");
  }
  // Per-tab claim (#184): fp + tab let the server's SessionRegistry recognise
  // owner vs secondary; force=1 demotes the current owner.
  if (opts?.fp) params.set("fp", opts.fp);
  if (opts?.tabId) params.set("tab", opts.tabId);
  if (opts?.force) params.set("force", "1");
  // Display-only device name for the take-over gate (#293). The server stores it on the
  // owner record and echoes it to other devices; it is never used for authorization.
  if (opts?.label) params.set("label", opts.label);
  // Initial grid (#227): tell the server our real size up front so the PTY (and a launched
  // agent) starts at the right width instead of 80x24 → reflow garbling on the first resize.
  if (opts?.cols) params.set("cols", String(opts.cols));
  if (opts?.rows) params.set("rows", String(opts.rows));
  return `${proto}://${location.host}/ws/term/${key}?${params.toString()}`;
}
