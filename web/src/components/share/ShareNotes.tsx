/** The notes of a memory item in a share bundle (#1714 P4, boards in the issue, comment 123445):
 *  each note as the preview judged it, and each note's outcome after the import. A note held back
 *  or refused shows no text of its own (the server never sent it): only its reason. */
import { useState } from "react";
import type { ShareNoteOutcome, ShareNotesResult, ShareNotesView } from "../../types/share";
import buttons from "../ui/actionButton.module.css";
import styles from "./share.module.css";

/** How many notes show before "Show all": a project may send a thousand. */
const FIRST = 5;

const TAG: Record<string, string> = { decision: "DEC", finding: "FND", pitfall: "PIT", reference: "REF" };

const VERDICT = {
  new: { word: "New", tone: styles.ok },
  held: { word: "Held back", tone: styles.warn },
  refused: { word: "Refused", tone: styles.down },
} as const;

export function ShareNotes({ view, label = "Notes in this file" }: { view: ShareNotesView; label?: string }) {
  const [all, setAll] = useState(false);
  const shown = all ? view.notes : view.notes.slice(0, FIRST);
  return (
    <div className={styles.notes} data-testid="share-notes">
      <ul aria-label={label}>
        {shown.map((n) => {
          const v = VERDICT[n.verdict] ?? VERDICT.refused;
          return (
            <li key={n.file} className={styles.note} data-verdict={n.verdict}>
              <span className={styles.tag}>{n.type ? TAG[n.type] ?? n.type : "—"}</span>
              <span className={styles.noteText}>
                {n.verdict === "new" ? (
                  <>
                    <b>{n.name}</b>
                    {n.description && n.description !== n.name ? ` — ${n.description}` : ""}
                  </>
                ) : n.verdict === "held" ? (
                  <span className={styles.why}>Name withheld</span>
                ) : (
                  <span className={styles.why}>{n.file}</span>
                )}
                {n.verdict !== "new" && (
                  <span className={`${styles.why} ${v.tone}`}>
                    {n.verdict === "held"
                      ? `${cap(n.reason ?? "held back")}. It is never stored and never put in a review tray; its text isn't shown.`
                      : `${cap(n.reason ?? "refused")}.`}
                  </span>
                )}
              </span>
              <span className={`${styles.stamp} ${v.tone}`}>{v.word}</span>
            </li>
          );
        })}
      </ul>
      {view.notes.length > FIRST && (
        <button type="button" className={buttons.ghost} aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? "Show fewer notes" : `Show all ${view.notes.length} notes`}
        </button>
      )}
    </div>
  );
}

const OUTCOME: Record<ShareNoteOutcome["outcome"], { word: string; tone: string }> = {
  created: { word: "Imported", tone: styles.ok },
  already: { word: "Already here", tone: "" },
  held: { word: "Held back", tone: styles.warn },
  refused: { word: "Refused", tone: styles.down },
  failed: { word: "Failed", tone: styles.down },
  retry: { word: "Failed", tone: styles.down },
  pending: { word: "Not reached", tone: "" },
};

/** The counts in words, and each note that wasn't simply imported, with why. */
export function ShareNotesOutcome({ tally, view }: { tally: ShareNotesResult; view?: ShareNotesView }) {
  const c = tally.counts;
  const name = new Map((view?.notes ?? []).map((n) => [n.file, n.name]));
  const words = [
    `${c.created} imported`,
    c.already ? `${c.already} already here` : "",
    c.held ? `${c.held} held back` : "",
    c.refused ? `${c.refused} refused` : "",
    c.failed + c.retry ? `${c.failed + c.retry} failed` : "",
    c.pending ? `${c.pending} not reached` : "",
  ].filter(Boolean);
  const notable = tally.notes.filter((n) => n.outcome !== "created").slice(0, 20);
  return (
    <>
      <span data-testid="share-notes-counts">{words.join(" · ")}</span>
      {notable.length > 0 && (
        <ul className={styles.noteOutcomes} aria-label="Notes not simply imported">
          {notable.map((n) => {
            const o = OUTCOME[n.outcome];
            return (
              <li key={n.file} data-outcome={n.outcome}>
                <span className={`${styles.stamp} ${o.tone}`}>{o.word}</span>{" "}
                {n.outcome === "held" ? "Name withheld" : name.get(n.file) ?? n.file}
                {n.outcome === "already" && n.slug ? ` (yours: ${n.slug})` : ""}
                {n.reason && n.outcome !== "already" ? ` — ${n.reason}` : ""}
                {n.outcome === "retry" ? " Retry failed items tries this note alone." : ""}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

const cap = (t: string) => `${t.slice(0, 1).toUpperCase()}${t.slice(1)}`.replace(/\.$/, "");
