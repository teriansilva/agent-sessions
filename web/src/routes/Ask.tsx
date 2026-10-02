import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { AskConsole } from "../components/ask/AskConsole";
import { NeedsYouDetailsDialog } from "../components/ask/NeedsYouDetailsDialog";
import { needsYouIds } from "../components/ask/needsYouLabels";
import { useNeedsYou } from "../components/ask/useNeedsYou";
import { useConfig } from "../app/config";
import { coerceRecentWindowDays } from "../lib/recentWindow";
import styles from "./Ask.module.css";

/** ASK's conversation page (#1171), under Dashboard in the nav the way the map sits under
 *  Sessions. The conversation is `AskConsole`; this route adds what it needs from around it:
 *
 *  - **The dashboard's question.** Asking on the dashboard navigates here with the question in
 *    router state. It is read ONCE, on the first render, and cleared from the history entry right
 *    away — a reload, or Back and Forward onto this entry, must never ask it again.
 *  - **NEEDS YOU membership,** so an answer row for a session on that list carries the marker and
 *    ⓘ (#1086), and the details dialog that ⓘ opens.
 *
 *  `configured` is the AI endpoint's own flag; Ask has no local fallback. */
export default function Ask() {
  const cfg = useConfig();
  const configured = cfg?.pulse?.configured ?? false;
  const windowDays = coerceRecentWindowDays(cfg?.pulse?.window_days);
  const { state, membership, refresh } = useNeedsYou(windowDays, "", "");
  const [details, setDetails] = useState<string | null>(null);

  const location = useLocation();
  const navigate = useNavigate();
  const [handedOff] = useState(() => {
    const q = (location.state as { ask?: unknown } | null)?.ask;
    return typeof q === "string" && q.trim() ? q.trim() : undefined;
  });
  // Consumed: the entry keeps its URL and loses the question. Once — `navigate` changes identity
  // with the location it just replaced, so without the ref this would chase itself.
  const cleared = useRef(false);
  useEffect(() => {
    if (!handedOff || cleared.current) return;
    cleared.current = true;
    navigate(location.pathname, { replace: true, state: null });
  }, [handedOff, location.pathname, navigate]);

  return (
    <div className={styles.page} data-testid="ask-page">
      <AskConsole
        configured={configured}
        needsYou={membership?.ids ?? needsYouIds(state.data?.rows)}
        onDetails={setDetails}
        initialQuestion={handedOff}
      />
      {details ? (
        <NeedsYouDetailsDialog
          key={details}
          sessionId={details}
          // Close only THIS dialog: a completion that lands later must never close a newer one.
          onClose={() => setDetails((cur) => (cur === details ? null : cur))}
          onChanged={() => void refresh()}
        />
      ) : null}
    </div>
  );
}
