/** The screen stays on while a call is connected. A phone's screen going dark hides the page, and
 *  a hidden page stopped hearing the Commander: on Android Chrome the call stayed up but nothing
 *  was heard until the screen was unlocked again. So the call holds a screen wake lock: the screen
 *  does not time out mid-call.
 *
 *  - Best-effort: a browser without the Wake Lock API, or one that refuses (battery saver), keeps
 *    the call as it was, with the device's own screen timeout.
 *  - The browser releases the lock whenever the page is hidden (the Commander switched apps or
 *    pressed the power button). It is asked for again each time the page is visible — including
 *    when the page hid and came back while a request was still pending (Hermes 7425): that
 *    request may be refused, or granted already released, so once it settles it is asked again.
 *    A plain refusal is never retried on its own: a browser that refuses keeps refusing.
 *  - The returned function lets go of it for good, a request still pending included. */
export function holdScreen(nav: Navigator = navigator, doc: Document = document): () => void {
  const wakeLock = nav.wakeLock;
  if (!wakeLock || typeof wakeLock.request !== "function") return () => undefined;
  let sentinel: WakeLockSentinel | null = null;
  let asking = false;
  /** The page became visible again while a request was pending: ask once more when it settles. */
  let missed = false;
  let done = false;

  const acquire = () => {
    if (done || doc.visibilityState !== "visible") return;
    if (sentinel && !sentinel.released) return;
    if (asking) {
      missed = true;
      return;
    }
    asking = true;
    missed = false;
    wakeLock.request("screen").then(
      (s) => {
        asking = false;
        if (done) {
          void s.release().catch(() => undefined); // the call ended while it was asked for
          return;
        }
        sentinel = s;
        if (missed) acquire(); // hidden meanwhile: a lock released already is asked for again
      },
      () => {
        asking = false;
        if (missed) acquire(); // refused while the page was away: try once now it is back
      },
    );
  };

  doc.addEventListener("visibilitychange", acquire);
  acquire();
  return () => {
    done = true;
    doc.removeEventListener("visibilitychange", acquire);
    if (sentinel && !sentinel.released) void sentinel.release().catch(() => undefined);
    sentinel = null;
  };
}
