import { useCallback, useEffect, useRef, useState } from "react";
import { useConfig } from "../app/config";
import { MissionConsole } from "../components/pulse/MissionConsole";
import { OrchestratorHealth } from "../components/pulse/OrchestratorHealth";
import { api } from "../lib/api";
import { OPERATOR_PENDING } from "../lib/orchestratorAction";
import type {
  OrchestratorAction,
  PulseOverview,
} from "../types/api";
import styles from "./Pulse.module.css";

// DEPTHS lived here for the route's FAST/MED/SLOW selector, removed with the scan chrome
// (#929). Scan depth is configured in Settings → Pulse, which still owns it.

/** One curated session card. ALL model-derived text (`synthesis`, `ai_summary`, the page
 *  banner, an Ask `why`) is rendered as plain text via React's default escaping — never
 *  markup — so a session title/summary can't inject into the page (#441). `why` (#522) is
 *  the Ask panel's one-line match reason, an optional extra row on the same card. */
// `Card` and `AskPanel` lived here. Both are gone with the grid (#878): a card is now a
// mission row in `MissionRail`, and Ask is `Composer` inside the mission thread. Their
// assertions did not go with them — see the PR's two-tier spec migration.

export default function Pulse() {
  const cfg = useConfig()?.pulse;
  const [overview, setOverview] = useState<PulseOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Bumped when an action is resolved from a card, so the orchestrator panel (which owns its
  // own pending/feed) re-reads rather than showing a count for something already decided.
  const [orchEpoch, setOrchEpoch] = useState(0);
  // Monotonic generation for overview writes. `reloadOverview` and `scanNow` race: an older
  // response arriving last would replace a newer one, and since the older snapshot predates a
  // rejection it would RESURRECT the settled action and its Approve/Reject buttons. The server
  // CAS still refuses the delivery, but the UI would be offering something already decided.
  const overviewGen = useRef(0);
  const applyOverview = useCallback((gen: number, o: PulseOverview) => {
    if (gen < overviewGen.current) return; // a newer write already landed
    overviewGen.current = gen;
    setOverview(o);
  }, []);
  // Re-fetch the cached overview. Deciding an action on a card removes it from the ledger's
  // live set, so the card must lose its controls without a reload (#754).
  const reloadOverview = useCallback(
    (settled?: OrchestratorAction) => {
      // Apply the settlement to the cards we already hold, BEFORE the refetch — and fence any
      // older response with the same generation counter. The GET can fail (offline, 5xx) and
      // its catch is deliberately silent, which left the settled action sitting on the card
      // while `ActionRow` cleared `busy` in its `finally` — so the controls came back enabled
      // for something the server had already decided, and stayed that way until a reload. The
      // decision is known from the response; it does not need a round trip to be shown.
      if (settled) {
        overviewGen.current += 1;
        setOverview((prev) => {
          if (!prev) return prev;
          const stillPending = OPERATOR_PENDING.has(settled.state);
          return {
            ...prev,
            cards: prev.cards.flatMap((c) => {
              if (c.pending_action?.id !== settled.id) return [c];
              // Mirror the server overlay: keep the row only while the action is still the
              // operator's to decide.
              if (stillPending) return [{ ...c, pending_action: settled }];
              // A card that exists only because the action did has nothing left to show —
              // dropping it beats leaving an empty phantom under "Needs you".
              if (c.synthesized_for_action) return [];
              // Undo the re-band too. `_attach_pending` overwrote `state` with `needs_you`
              // *because* of this action; with the action gone the band has to go back, or
              // the session sits under "Needs you" with nothing pending until some later
              // fetch succeeds — and the failing fetch is the case this whole branch exists
              // for.
              return [
                {
                  ...c,
                  pending_action: undefined,
                  state: c.state_without_action ?? c.state,
                },
              ];
            }),
          };
        });
      }
      const gen = ++overviewGen.current;
      api
        .pulse()
        .then((o) => applyOverview(gen, o))
        .catch(() => undefined);
      setOrchEpoch((n) => n + 1);
    },
    [applyOverview],
  );

  useEffect(() => {
    let live = true;
    const gen = ++overviewGen.current;
    api
      .pulse()
      .then((o) => live && applyOverview(gen, o))
      .catch(() => live && setError("Couldn’t load the overview."))
    return () => {
      live = false;
    };
  }, [applyOverview]);

  // `changeDepth` and `scanNow` were the route's scan controls and went with the header
  // (#929). `routes/PulseSettings.tsx` retains both the depth/window configuration and the
  // manual scan path, so nothing here is orphaned — only unreachable from this page.


  return (
    <div className={styles.pulse}>
      {/* ONE HEADER, AND NO SCAN VOCABULARY (#929).
          The window selector, the FAST/MED/SLOW depth and "Scan now" were Pulse's
          operator-triggered scan model, left behind when the dashboard was replaced. They are
          removed from the ROUTE only: `routes/PulseSettings.tsx` still owns `window_days`,
          `scan_depth` and the manual scan path, so the capability is untouched and this is a
          chrome removal rather than a feature retirement.

          The live-session count replaces the scan window as orientation. The MISSION count is
          deliberately not here: the console owns the mission list, and reaching into it from the
          route would mean a second fetch of the same thing purely to render a number. */}
      <header className={styles.head}>
        <div className={styles.headLeft}>
          {/* The feature's own name (#895). The route became `/mission` in #948; `/pulse` still
              redirects there, so old bookmarks and notification links keep working. */}
          <h1 className={styles.h1}>MISSION CONTROL</h1>
        </div>
      </header>

      {error && <p className={styles.err}>{error}</p>}

      {/* Narrow the whole list, not just the queue (#754) — the filters reach all sessions,
          including the ones the orchestrator has said nothing about, which is most of them. */}
      {/* THE AUTONOMY STRIP IS GONE (#929), and its own comment is why.
          It read: "The AUTONOMY strip, below the console (#878) … what you arrive to USE goes
          above what you arrive to READ" — while rendering ABOVE the console, doing the thing it
          forbade. It was also a second control surface for settings Settings already owned, and
          its empty state literally told the operator to go there.

          What did NOT move is the evidence. `OrchestratorHealth` is read-only and renders only
          when the orchestrator is failing: #772's distinction between "nothing needed you" and
          "nothing was CHECKED" is invisible on a quiet page, so an outage has to be legible on
          the page the operator is actually on, not only on the one that can fix it. */}
      <OrchestratorHealth refreshKey={orchEpoch} />

      {/* MISSION CONTROL (#878). This replaces the card grid, the state-of-your-work banner and
          the standalone Ask box — a card is now a mission row in the rail, the banner is the
          mission's own recap stream, and Ask is the composer. Nothing is orphaned by that: a
          live session is adopted into a mission from the session itself (#948). */}
      <MissionConsole
        allCards={overview?.cards ?? []}
        configured={cfg?.configured ?? false}
        onActionResolved={reloadOverview}
        onMembershipChanged={reloadOverview}
      />
    </div>
  );
}
