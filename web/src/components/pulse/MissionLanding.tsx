import type { ReactNode } from "react";
import { relTime } from "../../lib/format";
import type { MissionListRow } from "../../types/api";
import styles from "./mission.module.css";

/** How many needs-you missions the landing previews. The rail lists the rest. */
const PREVIEW = 3;

/** The Missions section's front door (#948 P3): what the workspace shows when no mission is
 *  selected — which is what entering the section now means.
 *
 *  The composer is the centrepiece and is passed in rather than owned here: the console keeps it
 *  mounted in one place so a half-typed brief survives the list arriving, a filter change, or the
 *  operator glancing at the rail (#930's draft-survival rule).
 *
 *  **NEEDS YOU is a PREVIEW of the rail, not a global attention surface.** Its rows are the rail's
 *  own loaded rows — the same retained filters, Active | Archived scope and page — so it shows
 *  exactly what the rail's dots show and costs no second fetch. Two consequences are said on
 *  screen rather than hidden:
 *
 *  - under any filter or the Archived scope the heading says "in current filters" and offers Clear
 *    filters, and an empty preview there reads "Nothing needs you in these filters";
 *  - when the rail holds only a page of a longer list the heading says "in the loaded missions".
 *
 *  It never says a bare "Nothing needs you": this list cannot know that. With no filter, a complete
 *  list and nothing needing the operator, the section is simply absent.
 *
 *  **An empty preview is a claim only after a SUCCESSFUL read** (#959 review 4805, finding 3). A
 *  filter or scope change clears the rail's rows before the new read answers, so `missions` is `[]`
 *  both for "nothing matched" and for "not answered yet" — and for "could not be read". Until the
 *  read succeeds the heading carries no count and the body says it is reading; when it failed, it
 *  says the missions could not be read. Clear filters stays offered in every filtered state.
 */
export function MissionLanding({
  composer,
  missions,
  filtered,
  partial,
  loaded,
  unavailable,
  onSelect,
  onClearFilters,
  projectNames,
}: {
  composer: ReactNode;
  /** The rail's loaded rows. Only those whose ledger says `needs_you` are previewed. */
  missions: MissionListRow[];
  /** A search / project / state filter, or the Archived scope, is narrowing the rail. */
  filtered: boolean;
  /** The rail holds a page of a longer list, or could not prove it is complete. */
  partial: boolean;
  /** A list read for the scope and filters on screen has answered (the console's `listLoaded`). */
  loaded: boolean;
  /** The list read failed, or answered with a store error. */
  unavailable: boolean;
  onSelect: (id: string) => void;
  /** Back to the Active scope with no filters. */
  onClearFilters: () => void;
  projectNames: Record<string, string>;
}) {
  const needy = missions.filter((m) => m.needs_you);
  // The two qualifications COMPOSE (#959 review 4814). A filter narrows what was asked for; a partial
  // page narrows what was read. The listing is newest-first, not attention-first, so a filtered first
  // page with no needy row says nothing about the filter's later pages.
  const scope =
    filtered && partial
      ? "in the loaded missions within current filters"
      : filtered
        ? "in current filters"
        : partial
          ? "in the loaded missions"
          : null;
  const emptyText = partial
    ? filtered
      ? "Nothing needs you in the loaded missions within these filters."
      : "Nothing needs you in the loaded missions."
    : "Nothing needs you in these filters.";
  const showNeeds = needy.length > 0 || filtered;
  /** Only a successful read may count, or say that nothing matched. */
  const answered = loaded && !unavailable;

  return (
    <div className={styles.landing} data-testid="mission-landing">
      <div className={styles.landingInner}>
        <h2 className={styles.landingHeading}>What should this mission achieve?</h2>
        <p className={styles.landingSub}>
          Mission control plans the work, dispatches an agent and drives it to a pull request.
        </p>
        {composer}
        <p className={styles.landingNote}>
          Already have a session running? Open it and use <b>Adopt to mission</b> in its header
          or its ⋯ menu.
        </p>
        {showNeeds ? (
          <section
            className={styles.landingNeeds}
            aria-label="Missions that need you"
            data-testid="landing-needs-you"
          >
            <div className={styles.landingNeedsHead}>
              <span className={`${styles.dot} ${styles.dotNeedsYou} ${styles.railDot}`} aria-hidden="true" />
              <span>
                Needs you{answered ? ` · ${needy.length}` : ""}
                {scope ? <span data-testid="landing-needs-scope"> · {scope}</span> : null}
              </span>
              {filtered ? (
                <button
                  type="button"
                  className={styles.landingClear}
                  onClick={onClearFilters}
                  data-testid="landing-clear-filters"
                >
                  Clear filters
                </button>
              ) : null}
            </div>
            {needy.length === 0 && unavailable ? (
              // No `role="status"`: the console already announces the store outage once.
              <div className={styles.landingEmpty} data-testid="landing-needs-error">
                Missions could not be read, so nothing is known about what needs you here.
              </div>
            ) : needy.length === 0 && !loaded ? (
              <div className={styles.landingEmpty} data-testid="landing-needs-loading">
                Reading missions…
              </div>
            ) : needy.length === 0 ? (
              <div className={styles.landingEmpty} data-testid="landing-needs-empty">
                {emptyText}
              </div>
            ) : (
              needy.slice(0, PREVIEW).map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className={styles.landingRow}
                  onClick={() => onSelect(m.id)}
                  data-testid="landing-needs-row"
                >
                  <span className={`${styles.dot} ${styles.dotNeedsYou} ${styles.railDot}`} role="img" aria-label="Needs you" />
                  <span className={styles.landingRowBody}>
                    <span className={styles.landingRowTitle}>{m.title}</span>
                    <span className={styles.landingRowMeta}>
                      {m.project_id ? projectNames[m.project_id] || "Unavailable project" : "No project"}
                      {" · "}
                      {m.session_keys.length} {m.session_keys.length === 1 ? "session" : "sessions"}
                      {" · "}
                      {relTime(m.updated_at)}
                    </span>
                  </span>
                </button>
              ))
            )}
          </section>
        ) : null}
      </div>
    </div>
  );
}
