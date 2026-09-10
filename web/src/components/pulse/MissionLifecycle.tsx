/** The mission's own controls: mark it running, close it, reopen it, archive it (#889).
 *
 * **Every transition is a compare-and-set and the UI must not paper over a lost one.** The route
 * takes `from` — the state the client *believes* the mission is in — because a state read before
 * an await cannot be trusted after it. A zero rowcount comes back as a **409**, and the honest
 * response is to re-read and render what the mission actually is, never the state the operator
 * asked for. A control that shows the requested state is the same class of lie as a probe that
 * re-serves a stale answer as current: it reports an intention as a fact.
 *
 * **The state machine is the server's, and this mirrors it rather than inventing a parallel one.**
 * `_ALLOWED` in `missions.py` is the whole graph; the buttons offered here are exactly the
 * transitions legal from the current state, so an offered control cannot 409 on "x cannot become
 * y". What it CAN still 409 on is the race — which is the case above, and is handled.
 *
 * **`planned -> running` is the adopted path.** A session the operator started themselves and then
 * adopted makes "work is underway" true without any dispatch having happened, and routing it
 * through `dispatching` would write a dispatch into the timeline that never occurred. The server
 * guards it: the mission must actually hold an active session, or the transition is refused.
 *
 * **Archive is destructive of RUNTIME, never of history**, and the confirm copy says exactly that
 * — masters and agent groups are terminated, every transcript is kept, and unarchive reverses it.
 * "Archive" reads as "delete my work" to anyone who has not read `mission_archive`.
 */
import { useCallback, useState } from "react";

import { ApiError, api } from "../../lib/api";
import type { Mission } from "../../types/api";

import { MissionOverflow } from "./MissionOverflow";
import styles from "./mission.module.css";

/** The transitions this surface offers, per state. A strict subset of the server's `_ALLOWED`:
 *  `dispatching` is Phase 4's and is not offered here, and `draft -> planned` is folded into the
 *  single BEGIN action below rather than being a button of its own. */
/** Reaching a TERMINAL state releases every session the mission holds, server-side
 *  (`missions.set_state`). So those transitions are membership changes as much as detach is, and
 *  the released sessions have to reappear under UNTRACKED — which is derived from the overview's
 *  cards, not from the mission list (finding 1 of #896's second review).
 *
 *  Module scope, not a value rebuilt per render: a fresh `Set` on every render is a fresh
 *  dependency, and the callback below would either be rebuilt every time or quietly omit it. */
const RELEASES_SESSIONS: ReadonlySet<string> = new Set([
  "done",
  "failed",
  "abandoned",
]);

const CLOSE_LABEL: Record<string, string> = {
  done: "MARK DONE",
  failed: "MARK FAILED",
};

export function MissionLifecycle({
  mission,
  onChanged,
  onNote,
}: {
  mission: Mission;
  /** A transition (or archive) landed, or a 409 told us the mission is not what we thought.
   *  Either way the console re-reads — the caller decides how.
   *
   *  `movedTo` says the mission has MOVED BETWEEN RAIL SCOPES (archive / unarchive) **and names
   *  the scope it moved INTO**, which the caller cannot infer from a re-read alone without
   *  racing it. Archiving from the active rail otherwise leaves the row gone from the list while
   *  the console still renders that mission's body — a rail and a body describing different
   *  sets, which is the exact failure `setScope` was written to prevent for the scope TOGGLE
   *  (finding 4 of #896's review).
   *
   *  **The direction is not inferable from the rail on screen** (#896 review 10, finding 6).
   *  "It left this rail" was enough while the only mover was archive-from-active; unarchive is
   *  the same event in the other direction, and a console that answers it by re-reading the rail
   *  it is showing re-reads Archived — the one scope the mission has just left. The destination
   *  is known HERE, by which button was pressed, and is carried rather than guessed.
   *
   *  **It is ONE field, not a `scopeChanged` boolean beside a `scopeTo`** (#896 review 14). Two
   *  fields that must agree can be forwarded apart, and they were: the console received the
   *  "which mission moved" half without the "where it went" half and removed the row from
   *  whichever rail was on screen — including the DESTINATION rail the mission had just
   *  correctly arrived in. One value cannot disagree with itself. */
  onChanged: (opts?: {
    movedTo?: "active" | "archived";
    membershipChanged?: boolean;
  }) => void;
  onNote: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  /** Which destructive action is awaiting its second tap. Confirmation is inline rather than a
   *  modal: the page's only focus trap is the rail drawer (#878) and it stays that way. */
  const [confirming, setConfirming] = useState<
    "abandon" | "archive" | "done" | "failed" | "unarchive" | null
  >(null);

  const state = mission.state;
  const archived = mission.archived_at != null;
  const holdsSession = (mission.sessions ?? []).some((s) => !s.removed_at);

  const act = useCallback(
    async (
      fn: () => Promise<unknown>,
      what: string,
      opts?: {
        movedTo?: "active" | "archived";
        membershipChanged?: boolean;
      },
    ) => {
      if (busy) return;
      setBusy(true);
      let ok = false;
      try {
        await fn();
        setConfirming(null);
        ok = true;
      } catch (err) {
        // The server's own `detail`, which is the answer here: "no longer planned" (the race),
        // "adopt a session before marking it running", "cannot become done without a resolved
        // cwd". `mutateJson` carries it precisely so the operator is told which (#834).
        onNote(
          err instanceof ApiError && err.message
            ? err.message
            : `${what} did not work.`,
        );
      } finally {
        setBusy(false);
        // ALWAYS re-read, success or failure. On a 409 this is the whole point: the mission is
        // not what this component believed, so the next render must come from the server rather
        // than from the state we asked for.
        //
        // **The VIEW-LOCAL effects are success-only** (#896 review 5, finding 3).
        // `movedTo` tells the console the mission left this rail, and it acts on that by
        // clearing the selection — so forwarding it from a REJECTED archive moves the operator
        // away from the very mission whose error they were just handed.
        //
        // **`membershipChanged` is not view-local and rides on failure too** (#896 review 10,
        // finding 5). It refreshes the OVERVIEW, and a lost CAS on a terminal transition is
        // positive evidence that it needs refreshing: the 409 says the mission is already in a
        // state this client did not put it in, which for a terminal state means the server has
        // ALREADY released its sessions. Suppressing the refresh there leaves those sessions in
        // neither the roster nor UNTRACKED — an ownership picture the server has just disproved
        // — until the outer poll happens to run. The transition not happening HERE is not the
        // same as it not having happened.
        const effects = {
          ...(ok ? opts : {}),
          ...(opts?.membershipChanged ? { membershipChanged: true } : {}),
        };
        onChanged(Object.keys(effects).length ? effects : undefined);
      }
    },
    [busy, onChanged, onNote],
  );

  const to = useCallback(
    (next: string, outcome?: string) =>
      act(
        () =>
          api.setMissionState(mission.id, {
            from: state,
            to: next,
            ...(outcome ? { outcome } : {}),
          }),
        `Moving to ${next}`,
        RELEASES_SESSIONS.has(next) ? { membershipChanged: true } : undefined,
      ),
    [act, mission.id, state],
  );

  if (archived) {
    return (
      <div className={styles.missionBar} data-testid="mission-lifecycle">
        <span className={styles.missionBarState} data-testid="mission-state">
          {state} · archived
        </span>
        {/* UNARCHIVE RELAUNCHES AGENTS, and until now it did so on one click with nothing on
            screen saying it would (#896 review 6, finding 4). `mission_archive` restores a
            session by relaunching it from its transcript, so an operator who read UNARCHIVE as
            "put the record back" could start several agents — with the cost and the side
            effects that implies. Two explicit choices now, and the safe one is offered first. */}
        {confirming === "unarchive" ? (
          <>
            <span
              className={styles.missionConfirm}
              data-testid="mission-confirm-unarchive"
            >
              Bringing this mission back can also RESTART its agents, by
              relaunching each session from its transcript.
            </span>
            <button
              type="button"
              className={styles.missionBtn}
              disabled={busy}
              onClick={() =>
                void act(
                  () => api.unarchiveMission(mission.id, { sessions: false }),
                  "Unarchiving",
                  { movedTo: "active" },
                )
              }
              data-testid="mission-unarchive-record"
            >
              RECORD ONLY
            </button>
            <button
              type="button"
              className={`${styles.missionBtn} ${styles.missionBtnDanger}`}
              disabled={busy}
              onClick={() =>
                void act(
                  () => api.unarchiveMission(mission.id, { sessions: true }),
                  "Unarchiving",
                  { movedTo: "active", membershipChanged: true },
                )
              }
              data-testid="mission-unarchive-sessions"
            >
              RESTART AGENTS
            </button>
          </>
        ) : (
          <button
            type="button"
            className={styles.missionBtn}
            disabled={busy}
            onClick={() => setConfirming("unarchive")}
            data-testid="mission-unarchive"
          >
            UNARCHIVE
          </button>
        )}
      </div>
    );
  }

  const terminal =
    state === "done" || state === "failed" || state === "abandoned";

  return (
    <div className={styles.missionBar} data-testid="mission-lifecycle">
      <span className={styles.missionBarState} data-testid="mission-state">
        {state}
      </span>

      {/* BEGIN — the adopted path. Two CAS calls, because they are two facts: a crash between
          them leaves the mission `planned`, which is true and recoverable. A single call that
          skipped `planned` would need `draft -> running` in the graph, and the graph deliberately
          refuses that: a draft has not been decided on yet. */}
      {state === "draft" ? (
        <button
          type="button"
          className={styles.send}
          disabled={busy}
          onClick={() => void to("planned")}
          data-testid="mission-plan"
        >
          READY
        </button>
      ) : null}

      {state === "planned" ? (
        <button
          type="button"
          className={styles.send}
          disabled={busy || !holdsSession}
          onClick={() => void to("running")}
          data-testid="mission-begin"
          title={
            holdsSession
              ? "Work is underway on this mission's session"
              : "Adopt a session first — a running mission with no session has nothing to follow through on"
          }
        >
          BEGIN
        </button>
      ) : null}

      {/* THE ONE PRIMARY, by state. `running`/`review` close as DONE; the other states each have
          their own next step above. Accent is reserved for exactly this control. */}
      {state === "running" || state === "review" ? (
        <button
          type="button"
          className={styles.send}
          disabled={busy}
          onClick={() =>
            confirming === "done" ? void to("done", "done") : setConfirming("done")
          }
          data-testid="mission-done"
        >
          {confirming === "done" ? `CONFIRM ${CLOSE_LABEL.done}` : CLOSE_LABEL.done}
        </button>
      ) : null}

      {/* REOPEN is the primary for a terminal mission — it is the only forward move left, so it
          takes the accent rather than sitting in the overflow with the destructive pair. */}
      {state === "done" || state === "failed" ? (
        <button
          type="button"
          className={styles.send}
          disabled={busy}
          onClick={() => void to("running")}
          data-testid="mission-reopen"
        >
          REOPEN
        </button>
      ) : null}

      {/* EVERYTHING BELOW IS SECONDARY, AND IT LIVES BEHIND `⋯` (#942).
          Four controls at near-equal weight — two of them red — is four shouts and no primary.
          The state's own next step stays inline above; the rest, including both destructive
          paths, collapse into a labelled menu. They keep their accessible names and their
          two-tap confirmations; only their prominence changes. */}
      <MissionOverflow busy={busy}>
      {state === "review" ? (
        <button
          type="button"
          className={styles.missionBtn}
          disabled={busy}
          onClick={() => void to("running")}
          role="menuitem"
          data-testid="mission-reopen-review"
        >
          NOT YET
        </button>
      ) : null}

      {/* CLOSING A MISSION RELEASES EVERY SESSION IT HOLDS, so it takes the same two taps the
          other destructive lifecycle actions take (#896 review 6, finding 2). #889 asks for
          "confirm done" in those words; the first version closed on the first click, so a
          mistap changed session ownership with nothing in between. */}
      {state === "running" || state === "review" ? (
        <button
          type="button"
          className={styles.missionBtn}
          disabled={busy}
          onClick={() =>
            confirming === "failed" ? void to("failed", "failed") : setConfirming("failed")
          }
          role="menuitem"
          data-testid="mission-failed"
        >
          {confirming === "failed" ? `CONFIRM ${CLOSE_LABEL.failed}` : CLOSE_LABEL.failed}
        </button>
      ) : null}

      {/* ABANDON is available from every non-terminal state — it is the operator saying "not
          this", and `_ALLOWED` accepts it from all of them. It is terminal in the strong sense:
          an abandoned mission cannot be reopened, which is why it confirms. */}
      {!terminal ? (
        <button
          type="button"
          className={`${styles.missionBtn} ${styles.missionBtnDanger}`}
          disabled={busy}
          onClick={() =>
            confirming === "abandon"
              ? void to("abandoned", "abandoned")
              : setConfirming("abandon")
          }
          role="menuitem"
          data-testid="mission-abandon"
        >
          {confirming === "abandon" ? "CONFIRM ABANDON" : "ABANDON"}
        </button>
      ) : null}

      {/* Archive is terminal-state-only; a live mission is a 409, not a prompt. The two-step
          `abandon: true` path exists for a live one, and it is only offered after saying plainly
          what it does. */}
      <button
        type="button"
        className={`${styles.missionBtn} ${terminal ? "" : styles.missionBtnDanger}`}
        disabled={busy}
        onClick={() =>
          confirming === "archive"
            ? void act(
                () => api.archiveMission(mission.id, { abandon: !terminal }),
                "Archiving",
                { movedTo: "archived", membershipChanged: true },
              )
            : setConfirming("archive")
        }
        role="menuitem"
        data-testid="mission-archive"
      >
        {confirming === "archive" ? "CONFIRM ARCHIVE" : "ARCHIVE"}
      </button>
      </MissionOverflow>

      {confirming === "abandon" ? (
        <p
          className={styles.missionConfirm}
          data-testid="mission-confirm-abandon"
        >
          Abandoning closes this mission and releases its sessions. It cannot be
          reopened.
        </p>
      ) : null}
      {confirming === "archive" ? (
        <p
          className={styles.missionConfirm}
          data-testid="mission-confirm-archive"
        >
          {terminal
            ? "Archiving stops this mission's agents and frees their terminals. Every transcript is kept, and UNARCHIVE brings it back."
            : "This mission is still live. Archiving it will abandon it first — stopping its agents and freeing their terminals. Every transcript is kept, and UNARCHIVE brings it back."}
        </p>
      ) : null}
    </div>
  );
}
