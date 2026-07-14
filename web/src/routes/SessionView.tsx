import { useCallback, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Terminal } from "../components/terminal/Terminal";
import type { FreshSession } from "../lib/termUrl";

/** Session view at "/s/:engine/:id" — the URL is the single source of truth for which
 *  session is open (deep-linkable, refresh-safe). When arrived at from the new-session
 *  landing, router state carries the fresh-launch params (cwd + bypass) so the terminal
 *  opens it with ?new=1; a direct deep-link / reload has no state → a plain attach.
 *
 *  opencode new-session converge (#127): opencode mints its own id, so we open under a
 *  client placeholder (`/s/opencode/new-<uuid>`); when the server reconciles to the real
 *  `ses_…`, the terminal calls `onReconcileId` and we replace the URL to
 *  `/s/opencode/ses_…` (history replace, no reload) and DROP the fresh state (a later
 *  reload is now a plain attach by the real id). The displayed terminal keeps its ORIGINAL
 *  identity (the placeholder), so it is never remounted — the live socket is kept, no
 *  relaunch, no flicker. */
export function SessionView() {
  const { engine, id } = useParams<{ engine: string; id: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const fresh = (location.state as { fresh?: FreshSession } | null)?.fresh;
  const liveKey = `${engine ?? ""}:${id ?? ""}`;

  // React Router reuses this component instance across a param-only change (no remount),
  // so we can't read the URL params directly for the terminal identity — our own
  // placeholder→real converge changes the params but must NOT re-key the terminal (that
  // would tear down the live socket). We hold the *displayed* identity in state and only
  // re-seed it on a genuine navigation. `converged` holds the real id(s) we ourselves
  // navigated to via reconcile; a param change matching one is OUR converge and keeps the
  // frozen identity. (A set, not a single value, so the keep-frozen decision is independent
  // of the order in which the param update and the state update commit.) It is cleared the
  // moment we adopt a genuine navigation, so the suppression is one-shot and scoped to the
  // still-mounted placeholder — a later navigation back to a reconciled real id re-opens it.
  const [shown, setShown] = useState({ engine: engine ?? "", id: id ?? "" });
  const [converged, setConverged] = useState<Set<string>>(() => new Set());

  // Derived-state reconciliation (render-safe): adopt the new params unless this is one of
  // our own converges. setState-during-render is the supported React pattern for adjusting
  // state to a prop change without an extra commit+effect round-trip.
  const shownKey = `${shown.engine}:${shown.id}`;
  if (liveKey !== shownKey && engine && id && !converged.has(liveKey)) {
    // A real navigation to a different session → show it (the new key remounts Terminal).
    setShown({ engine, id });
    // Drop the converge-suppression now that we've left the placeholder: it only ever guards
    // the in-place placeholder→real swap of the *currently shown* session. Without this, a
    // real id stayed in the set forever, so navigating away and later BACK to that same real
    // opencode URL would be wrongly treated as our converge and refused — the terminal would
    // stay on the other session while the address bar showed the opencode one (Hermes #131).
    if (converged.size) setConverged(new Set());
  }

  const onReconcileId = useCallback(
    (sid: string) => {
      // sid is the real engine-qualified id ("opencode:ses_…"); convert to the route.
      const [reEngine, ...rest] = sid.split(":");
      const reId = rest.join(":");
      if (!reEngine || !reId) return;
      // Remember this real id as OUR converge target so the upcoming param change keeps the
      // frozen terminal identity (live socket preserved). Replace the URL in place (no
      // history push, no reload) and drop the fresh-launch state so a subsequent reload
      // attaches by the real id rather than re-launching.
      setConverged((prev) => new Set(prev).add(`${reEngine}:${reId}`));
      navigate(`/s/${reEngine}/${reId}`, { replace: true, state: null });
    },
    [navigate],
  );

  if (!shown.engine || !shown.id) return null;
  return (
    <Terminal
      key={`${shown.engine}:${shown.id}`}
      engine={shown.engine}
      id={shown.id}
      fresh={fresh}
      onReconcileId={onReconcileId}
    />
  );
}
