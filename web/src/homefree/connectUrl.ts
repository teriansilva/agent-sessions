/** Where the address bar goes once the relay connects (#1735).
 *
 *  The public connect page is served under `/connect/`, and the app it mounts routes under the same
 *  base, so a deep path (`/connect/s/<engine>/<id>`, `/connect/mission?m=<id>`) is the route the
 *  operator reloaded on. It is kept, so that after the connect gate the app opens there again.
 *  A box's own `/connect.html` keeps its old behaviour: its app is the local build at `/`.
 *
 *  Only the query keys that are app ROUTE state survive (an allowlist). Nothing else in the query
 *  or the hash is carried into the app's address bar: the shell reads no credential from the URL,
 *  but a stray credential-shaped parameter must never outlive the connect. */

/** Query keys that are route state: `m` is the mission (`/mission?m=<id>`). */
export const ROUTE_QUERY_KEYS: readonly string[] = ["m"];

export function connectedUrl(
  loc: { pathname: string; search: string },
  publicDeploy: boolean,
): string {
  const underBase = loc.pathname === "/connect" || loc.pathname.startsWith("/connect/");
  if (!underBase) return publicDeploy ? "/connect/" : "/connect.html";
  const path =
    loc.pathname === "/connect" || loc.pathname === "/connect/index.html"
      ? "/connect/"
      : loc.pathname;
  const from = new URLSearchParams(loc.search);
  const kept = new URLSearchParams();
  for (const key of ROUTE_QUERY_KEYS) {
    const value = from.get(key);
    if (value !== null) kept.set(key, value);
  }
  const query = kept.toString();
  return query ? `${path}?${query}` : path;
}
