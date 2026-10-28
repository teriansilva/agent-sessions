/** The app's URL base (#1735). The local build serves at `/`; the relay's connect build is served
 *  under `/connect/` on the landing host, where everything outside that prefix redirects away. So
 *  every URL the app shows, follows or reloads has to carry the base, or a reload 404s.
 *
 *  Route paths everywhere stay root-relative (`/s/…`, `/mission`): the router's `basename` adds the
 *  prefix, and the few places that touch `window.location` directly go through these helpers. */

/** `""` in the local build, `"/connect"` in the connect build. Never a trailing slash. */
export const APP_BASENAME = import.meta.env.BASE_URL.replace(/\/+$/, "");

/** A root-relative app path as a URL under the base: `/login` → `/connect/login` on the relay. */
export function appUrl(path: string): string {
  return APP_BASENAME + path;
}

/** A location pathname as the app's own route path: `/connect/s/x/y` → `/s/x/y` on the relay. A
 *  path outside the base is returned unchanged (it is not an app route there, and callers then
 *  find no match). */
export function stripBase(pathname: string): string {
  if (!APP_BASENAME) return pathname;
  if (pathname === APP_BASENAME) return "/";
  return pathname.startsWith(`${APP_BASENAME}/`) ? pathname.slice(APP_BASENAME.length) : pathname;
}
