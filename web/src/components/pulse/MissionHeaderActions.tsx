/** The mission header's actions: ONE owner, ONE row (#889, #942, #967).
 *
 * **One owner.** The header used to have two. `MissionLifecycle` portalled the state, the close or
 * reopen primary and ⋯ into the header's slot, and `MissionPlanCard` portalled Begin, Re-plan and a
 * reason paragraph into the same slot, jumping ahead of them with `order: -1` inside a 460px box. Two
 * components deciding one row is how Begin, Re-plan, a tiny `draft` label, ⋯ and a sentence came to
 * be spread over two lines. This component draws the whole row from ONE model: the mission's state,
 * plus the start model the mission body shares with the plan card (`useMissionStart`).
 *
 * **One row.** Left to right:
 *
 * - a STATE CHIP: a 6px status dot and the rail's own label (`missionState.ts`), so a state reads the
 *   same in the list and here. Status tokens colour the dot, a failed label is `--danger-text`, and
 *   the accent never means a state (§3);
 * - AT MOST ONE PRIMARY, by state: Begin for draft and planned (disabled until the plan and the
 *   objectives are ready), Starting… while dispatching, Mark done as a ghost while running or in
 *   review, Reopen for done and failed, Unarchive for an archived mission;
 * - ⋯ for everything else: Plan again, Review plan, Not yet, Mark failed, Abandon and Archive. Each
 *   keeps the testid it had as a button.
 *
 * Nothing that EXPLAINS goes in the row. The start reason lives in the plan card, next to the plan it
 * describes, and Begin points at it with `aria-describedby`. The two-tap confirmations for Abandon and
 * Archive render in the menu, under the item that asked. The Unarchive choice opens in a panel
 * anchored under the row.
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
import {
  Archive,
  Ban,
  CircleX,
  FileText,
  RotateCw,
  Undo2,
} from "lucide-react";
import { useCallback, useState, type ReactNode } from "react";

import { ApiError, api } from "../../lib/api";
import type { Mission } from "../../types/api";

import action from "../ui/actionButton.module.css";
import { MissionOverflow } from "./MissionOverflow";
import { missionDotClass, missionStateLabel } from "./missionState";
import styles from "./mission.module.css";
import {
  LAUNCH_WARNING_ID,
  START_REASON_ID,
  type MissionStart,
} from "./useMissionStart";

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

export function MissionHeaderActions({
  mission,
  start,
  onChanged,
  onNote,
}: {
  mission: Mission;
  /** The body's start model: Begin and Plan again. Optional only so the lifecycle half can be
   *  mounted on its own in a unit test; the console always passes it. */
  start?: MissionStart;
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
  const terminal =
    state === "done" || state === "failed" || state === "abandoned";

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

  /* THE STATE CHIP. The label is text and the dot is decoration, so colour is never the only signal. */
  const failed = state === "failed";
  const chip = (
    <span
      className={`${styles.stateChip} ${failed ? styles.stateChipFailed : ""}`}
      data-testid="mission-state-chip"
    >
      <span
        className={`${styles.dot} ${missionDotClass(state)} ${styles.chipDot}`}
        aria-hidden="true"
        data-testid="mission-state-dot"
      />
      <span data-testid="mission-state">
        {archived
          ? `${missionStateLabel(state)} · archived`
          : missionStateLabel(state)}
      </span>
    </span>
  );

  /* THE ONE PRIMARY, by state. Accent is reserved for a forward move; Mark done is a ghost because
     closing a live mission is a decision, not the next step (#967). */
  let primary: ReactNode = null;
  if (archived) {
    // UNARCHIVE RELAUNCHES AGENTS, and until #896 review 6 (finding 4) it did so on one click with
    // nothing on screen saying it would. `mission_archive` restores a session by relaunching it
    // from its transcript, so the button only OPENS the choice, and the safe option is offered first.
    primary = (
      <button
        type="button"
        className={action.primary}
        disabled={busy}
        aria-expanded={confirming === "unarchive"}
        onClick={() =>
          setConfirming(confirming === "unarchive" ? null : "unarchive")
        }
        data-testid="mission-unarchive"
      >
        Unarchive
      </button>
    );
  } else if (
    start &&
    (state === "draft" || state === "planned" || state === "dispatching")
  ) {
    primary = (
      <button
        type="button"
        className={action.primary}
        disabled={start.beginDisabled}
        onClick={() => void start.begin()}
        // The reason renders in the plan card, which exists exactly while the mission is plannable;
        // once armed, the launch warning there describes "Confirm begin" instead.
        aria-describedby={
          start.active
            ? start.validConfirmation
              ? LAUNCH_WARNING_ID
              : START_REASON_ID
            : undefined
        }
        data-testid="mission-begin"
      >
        {start.beginLabel}
      </button>
    );
  } else if (state === "running" || state === "review") {
    // CLOSING A MISSION RELEASES EVERY SESSION IT HOLDS, so it takes two taps (#896 review 6,
    // finding 2). #889 asks for "confirm done" in those words; the first version closed on the
    // first click, so a mistap changed session ownership with nothing in between.
    primary = (
      <button
        type="button"
        className={action.ghost}
        disabled={busy}
        onClick={() =>
          confirming === "done"
            ? void to("done", "done")
            : setConfirming("done")
        }
        data-testid="mission-done"
      >
        {confirming === "done" ? "Confirm mark done" : "Mark done"}
      </button>
    );
  } else if (state === "done" || state === "failed") {
    // REOPEN is the primary for a closed mission — it is the only forward move left.
    primary = (
      <button
        type="button"
        className={action.primary}
        disabled={busy}
        onClick={() => void to("running")}
        data-testid="mission-reopen"
      >
        Reopen
      </button>
    );
  }

  /* EVERYTHING ELSE IS SECONDARY, AND IT LIVES BEHIND `⋯` (#942, #967). Several controls at
     near-equal weight, some of them red, is several shouts and no primary. They keep their
     accessible names, their testids and their two-tap confirmations; only their prominence changes.
     An archived mission has nothing here but Unarchive, so it has no menu at all. */
  const planning = !archived && start?.active ? start : null;
  /** Anything above the destructive group, so the hairline has something to separate. */
  const hasLead = planning !== null || state === "running" || state === "review";
  const icon = (node: ReactNode) => (
    <span className={styles.menuIcon} aria-hidden="true">
      {node}
    </span>
  );
  const menu = archived ? null : (
    <MissionOverflow
      busy={busy}
      note={
        confirming === "abandon" ? (
          <p
            className={styles.missionConfirm}
            data-testid="mission-confirm-abandon"
          >
            Abandoning closes this mission and releases its sessions. It cannot
            be reopened.
          </p>
        ) : confirming === "archive" ? (
          <p
            className={styles.missionConfirm}
            data-testid="mission-confirm-archive"
          >
            {terminal
              ? "Archiving stops this mission's agents and frees their terminals. Every transcript is kept, and Unarchive brings it back."
              : "This mission is still live. Archiving it will abandon it first — stopping its agents and freeing their terminals. Every transcript is kept, and Unarchive brings it back."}
          </p>
        ) : null
      }
    >
      {/* PLAN AGAIN is what Re-plan was (#967): it prepares a proposal and starts nothing. */}
      {planning ? (
        <button
          type="button"
          className={styles.menuItem}
          disabled={planning.disabled}
          onClick={planning.propose}
          role="menuitem"
          data-testid="mission-replan"
        >
          {icon(<RotateCw size={15} />)}
          {planning.busy === "plan" ? "Planning…" : "Plan again"}
        </button>
      ) : null}
      {planning?.plan && planning.canReviewPlan ? (
        <button
          type="button"
          className={styles.menuItem}
          onClick={planning.reviewPlan}
          role="menuitem"
          data-testid="mission-review-plan"
        >
          {icon(<FileText size={15} />)}
          Review plan
        </button>
      ) : null}

      {state === "review" ? (
        <button
          type="button"
          className={styles.menuItem}
          disabled={busy}
          onClick={() => void to("running")}
          role="menuitem"
          data-testid="mission-reopen-review"
        >
          {icon(<Undo2 size={15} />)}
          Not yet
        </button>
      ) : null}

      {state === "running" || state === "review" ? (
        <button
          type="button"
          className={styles.menuItem}
          disabled={busy}
          onClick={() =>
            confirming === "failed"
              ? void to("failed", "failed")
              : setConfirming("failed")
          }
          role="menuitem"
          data-testid="mission-failed"
        >
          {icon(<Ban size={15} />)}
          {confirming === "failed" ? "Confirm mark failed" : "Mark failed"}
        </button>
      ) : null}

      {/* The hairline before the destructive group, as RowMenu draws one. Not a menuitem, so the roving
          focus skips it. */}
      {hasLead ? <div className={styles.menuSep} role="separator" /> : null}

      {/* ABANDON is available from every non-terminal state — it is the operator saying "not
        this", and `_ALLOWED` accepts it from all of them. It is terminal in the strong sense:
        an abandoned mission cannot be reopened, which is why it confirms. */}
      {!terminal ? (
        <button
          type="button"
          className={`${styles.menuItem} ${styles.menuDanger}`}
          disabled={busy}
          onClick={() =>
            confirming === "abandon"
              ? void to("abandoned", "abandoned")
              : setConfirming("abandon")
          }
          role="menuitem"
          data-testid="mission-abandon"
        >
          {icon(<CircleX size={15} />)}
          {confirming === "abandon" ? "Confirm abandon" : "Abandon"}
        </button>
      ) : null}

      {/* Archive is terminal-state-only; a live mission is a 409, not a prompt. The two-step
        `abandon: true` path exists for a live one, and it is only offered after saying plainly
        what it does. */}
      <button
        type="button"
        className={`${styles.menuItem} ${terminal ? "" : styles.menuDanger}`}
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
        {icon(<Archive size={15} />)}
        {confirming === "archive" ? "Confirm archive" : "Archive"}
      </button>
    </MissionOverflow>
  );

  return (
    <div className={styles.headerActions} data-testid="mission-lifecycle">
      {chip}
      {primary}
      {menu}
      {archived && confirming === "unarchive" ? (
        <div
          className={styles.headerConfirm}
          role="group"
          aria-label="Bring this mission back"
        >
          <p
            className={styles.missionConfirm}
            data-testid="mission-confirm-unarchive"
          >
            {/* The capitals are the warning (#896 review 6, finding 4), kept through #967's recasing. */}
            Bringing this mission back can also RESTART its agents, by
            relaunching each session from its transcript.
          </p>
          <div className={styles.headerConfirmActions}>
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
              Record only
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
              Restart agents
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
