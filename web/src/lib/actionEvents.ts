/** Same-tab notification invalidation (#800).
 *
 *  Resolving an orchestrator action retires its bell row on the server, but the bell polls on a
 *  60s timer — so without a nudge the badge keeps counting an escalation the operator just
 *  decided, for up to a minute, on the very screen where they decided it.
 *
 *  Deliberately its OWN module rather than an export of `lib/api`:
 *
 *  * A listener needs the event name, not the API client. `NotificationBell` importing `api`
 *    for a string constant would be backwards.
 *  * Test files mock `lib/api` with a factory (`vi.mock("../lib/api", () => ({ api: {...} }))`),
 *    which replaces the module wholesale — any constant living there is `undefined` inside those
 *    suites and Vitest throws on the missing named export. A constant that every future api mock
 *    has to remember to re-declare is a constant in the wrong file.
 *
 *  Same tab only. Other tabs and other devices converge on their next poll or next open; making
 *  that immediate would need a push or socket channel this does not justify.
 */
export const ACTION_RESOLVED_EVENT = "agent-sessions:action-resolved";

/** Announce that an action was resolved, then hand the record straight back.
 *
 *  Shaped as a pass-through so it can sit on the end of a promise chain without changing what
 *  the caller sees. It never throws: the action HAS been resolved by the time this runs, and a
 *  missing or odd `window` (SSR, a partial test double) must not turn a successful decision into
 *  a rejected promise.
 */
export function announceActionResolved<T>(rec: T): T {
  try {
    window.dispatchEvent(new CustomEvent(ACTION_RESOLVED_EVENT));
  } catch {
    // no-op — the decision stands regardless of whether anything is listening
  }
  return rec;
}
