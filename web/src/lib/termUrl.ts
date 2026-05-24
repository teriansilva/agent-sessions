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

export function termWsUrl(engine: string, id: string, have: number, fresh?: FreshSession): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const key = `${encodeURIComponent(engine)}:${encodeURIComponent(id)}`;
  const params = new URLSearchParams({ have: String(have) });
  if (fresh) {
    params.set("new", "1");
    params.set("cwd", fresh.cwd);
    params.set("bypass", fresh.bypass ? "1" : "0");
  }
  return `${proto}://${location.host}/ws/term/${key}?${params.toString()}`;
}
