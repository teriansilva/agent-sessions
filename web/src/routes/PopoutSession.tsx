import { useEffect } from "react";
import { useParams } from "react-router-dom";
import { useSessionRow } from "../app/useSessionRow";
import { PopoutContext } from "../lib/popout";
import { SessionView } from "./SessionView";
import styles from "./PopoutSession.module.css";

/** A session popped out into its own window or tab (`/pop/:engine/:id`): the session view alone,
 *  rendered OUTSIDE the app shell — no session list, section nav, top bar or footer. The router
 *  mounts it beside the shell's catch-all, so none of the shell's chrome exists here.
 *
 *  It is the same `SessionView` the main window shows (terminal or API pane, its head, Files), told
 *  through `PopoutContext` that it is popped out: it offers no "To map" and keeps its in-place URL
 *  moves on this route, and the session menu offers no "Pop out". It ATTACHES like any second tab
 *  on the session; it never launches one (no `fresh` state ever reaches it). */
export function PopoutSession() {
  const { engine = "", id = "" } = useParams<{ engine: string; id: string }>();
  const row = useSessionRow(`${engine}:${id}`);
  const name = row?.title || (id ? `${id.slice(0, 8)}…` : "Session");
  // The window's own name in the task bar / tab strip, so several pop-outs tell apart.
  useEffect(() => {
    const before = document.title;
    document.title = `${name} · BattleLab`;
    return () => {
      document.title = before;
    };
  }, [name]);
  return (
    <PopoutContext.Provider value={true}>
      <main className={`terminal-pane ${styles.popout}`} aria-label={`Session ${name}`}>
        <SessionView />
      </main>
    </PopoutContext.Provider>
  );
}
