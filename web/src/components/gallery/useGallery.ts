/** The stored gallery listing, checked against the public gallery once per page load (#1440), so
 *  nobody has to press a refresh button for it to be current. The check is the existing anonymous,
 *  signed refresh (`POST /api/gallery/refresh`); a failure leaves the stored listing as it was. */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, csrfReady } from "../../lib/api";
import { usePlaybookRead } from "../playbooks/usePlaybooks";
import { errorText } from "./operations";

export type GalleryProblem =
  /** The origin did not answer (not published yet, offline, busy). */
  | { kind: "unreachable"; detail: string }
  /** It answered with something that failed verification; nothing changed. */
  | { kind: "refused"; detail: string };

let checkedThisPage = false;
/** The last check's outcome, so returning to the view still says why the listing is old/empty. */
let lastProblem: GalleryProblem | null = null;
/** A check still running when the view remounts (navigating list ↔ entry) is awaited, not lost. */
let inflight: Promise<void> | null = null;
/** Tests reset the once-per-page check between renders. */
export function resetGalleryCheck() {
  checkedThisPage = false;
  lastProblem = null;
  inflight = null;
}

/** How long the automatic check waits for the app's session token before giving up for now. */
export const CSRF_WAIT_MS = 8000;

async function refresh(): Promise<void> {
  // The check starts on mount, possibly before the app's first config read set the token. Wait
  // for it, but not forever: without a token the check reads as "can't be reached" and Try again
  // works once the app is ready. Writes stay held either way.
  const ready = await Promise.race([
    csrfReady().then(() => true),
    new Promise<boolean>((resolve) =>
      setTimeout(() => resolve(false), CSRF_WAIT_MS),
    ),
  ]);
  if (!ready) {
    lastProblem = {
      kind: "unreachable",
      detail:
        "The app's session wasn't ready yet, so the gallery wasn't checked.",
    };
    return;
  }
  try {
    await api.refreshGallery();
    lastProblem = null;
  } catch (e) {
    // Only a 422 means the gallery answered with content that failed verification. A 503, a
    // refused request (403) or a network failure is "can't be reached right now".
    lastProblem =
      e instanceof ApiError && e.status === 422
        ? { kind: "refused", detail: errorText(e) }
        : { kind: "unreachable", detail: errorText(e) };
  }
}

/** Join (or start) this page's one check and return its outcome — for code outside a view, such as
 *  the old-link redirect, that must not act on a listing the current check could not confirm. */
export async function ensureChecked(): Promise<GalleryProblem | null> {
  if (!inflight && !checkedThisPage) {
    checkedThisPage = true;
    inflight = refresh();
  }
  const running = inflight;
  if (running) {
    await running;
    if (inflight === running) inflight = null;
  }
  return lastProblem;
}

/** Why a write must wait for the public gallery, or "" when the current check confirmed it AND the
 *  listing was read back after it. A listing kept from an earlier check or an earlier read is still
 *  shown, but never acted on (#1440). */
export function galleryWait(gallery: {
  checking: boolean;
  problem: GalleryProblem | null;
  /** A read of the stored listing is in flight: what is shown may predate the check. */
  loading: boolean;
  /** The latest read of the stored listing failed: what is shown may predate the check. */
  error: string | null;
}): string {
  if (gallery.checking) return "Checking the public gallery…";
  if (gallery.loading) return "Loading the gallery listing…";
  if (gallery.error)
    return "The gallery listing couldn't be loaded, so this waits until it can.";
  if (gallery.problem?.kind === "unreachable")
    return "The public gallery can't be reached right now, so this waits until it can.";
  if (gallery.problem)
    return "The gallery listing couldn't be verified, so this waits until it can.";
  return "";
}

export function useGallery() {
  const read = usePlaybookRead(useCallback(() => api.publicGallery(), []));
  const { reload } = read;
  // Mounting either joins a running check or starts the page's first one, so both read "checking".
  const [checking, setChecking] = useState(
    () => inflight !== null || !checkedThisPage,
  );
  const [problem, setProblem] = useState<GalleryProblem | null>(
    () => lastProblem,
  );
  /** Await a check; state changes only after it settles (never synchronously in the effect). */
  const settle = useCallback(
    async (running: Promise<void>) => {
      // "checking" holds until the listing read AFTER a successful check has completed: in between,
      // the listing on screen is the one from before the check and must not be acted on.
      try {
        await running;
        if (inflight === running) inflight = null;
        setProblem(lastProblem);
        if (!lastProblem) await reload();
      } finally {
        if (inflight === running) inflight = null;
        setChecking(false);
      }
    },
    [reload],
  );
  const check = useCallback(() => {
    setChecking(true);
    return settle((inflight ??= refresh()));
  }, [settle]);
  useEffect(() => {
    if (!inflight && checkedThisPage) return;
    checkedThisPage = true;
    void settle((inflight ??= refresh()));
  }, [settle]);
  return { ...read, checking, problem, check };
}
