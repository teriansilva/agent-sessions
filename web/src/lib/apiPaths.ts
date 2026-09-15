/** API route paths named in more than one file: the client and the browser tests that mock it.
 *
 *  One exported string per route, so a renamed route is one edit and a spec can never mock a path
 *  the app no longer calls. Kept free of imports so a Playwright spec can load it without pulling in
 *  the browser-only client. */

/** `POST` — what a direction would type, filled with example facts (#983 P2). Read-only. */
export const DIRECTION_PREVIEW_PATH = "/api/mission-directions/preview";
