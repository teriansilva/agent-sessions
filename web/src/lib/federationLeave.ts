/** Leaving a share (#2201): the receiving instance gives up ONE session a peer shares with it.
 *  Pure: the failure classes, the dialog's state and its words. The component is
 *  `components/federation/useLeaveShare.tsx`; both places that offer Leave use it.
 *
 *  What each answer proves, and it is the server's rule (`federation/remote.py` `leave`):
 *
 *  - `200`: the share ended on the owner. The only proof there is. The row goes.
 *  - `503 peer_offline`: the request never left this instance. Nothing changed anywhere.
 *  - `404 no_grant`: a refusal, which proves NOTHING. A share whose revision moved answers it
 *    while it is still live. The list is read again; the row stays unless the list no longer has
 *    it.
 *  - everything else (`504 peer_lost`, a timeout, `429 busy`, the network, any status or code
 *    nobody listed): not known. The row stays, and the only thing offered is a READ of the list.
 *
 *  The classes come from `writeFailure` (`federation.ts`): there is no second table here. */
import { writeFailure, type RemoteShare } from "./federation";

/** `refused`: `404 no_grant`. `offline`: it never left. `rejected`: another proven refusal
 *  (nothing changed; the server's words say why). `unconfirmed`: everything else. */
export type LeaveFailure = "refused" | "offline" | "rejected" | "unconfirmed";

export function leaveFailure(status: number, code: unknown): LeaveFailure {
  const failure = writeFailure(status, code);
  if (failure === "refused" || failure === "offline" || failure === "unconfirmed") return failure;
  return "rejected"; // `held` cannot happen for a leave; any proven refusal reads the same here
}

/** What a dialog's state is FOR: one share at one revision. A state made for another key is
 *  not this share's state (`leaveShown`): another share, or the same share after the owner
 *  changed it, starts clean. */
export const leaveKey = (r: Pick<RemoteShare, "peer_id" | "grant_id" | "revision">): string =>
  `${r.peer_id}\u0000${r.grant_id}\u0000${r.revision}`;

/** A settled state of the dialog. `confirm` is the question; the rest say what one Leave came
 *  to. (A request in flight is not a state here: the component holds it, and it is not bound to
 *  a revision, so a list read during it can never re-enable the button.) */
export type LeavePhase = "confirm" | "offline" | "refused" | "rejected" | "unconfirmed";

export interface LeaveState {
  /** The share and revision this state was made for, or `null` for none. */
  key: string | null;
  phase: LeavePhase;
  /** Whether this dialog sent a Leave since it was opened: only then does a share that leaves
   *  the list mean "you left" for whoever opened the dialog. */
  sent: boolean;
  /** After Check again: the list was read and still has the share, or it could not be read. */
  checked: "listed" | "unread" | null;
  /** The server's sentence for a proven refusal that is not `no_grant` (plain text). */
  message: string | null;
}

export const LEAVE_IDLE: LeaveState = Object.freeze({
  key: null,
  phase: "confirm",
  sent: false,
  checked: null,
  message: null,
});

export type LeaveEvent =
  /** The dialog was opened (for any share): nothing of an earlier dialog carries over. */
  | { type: "open" }
  /** A Leave left the browser. */
  | { type: "sent" }
  /** A Leave's answer that is not `ok`, with the key the list shows when it is applied. */
  | { type: "failed"; key: string | null; failure: LeaveFailure; message?: string | null }
  /** Check again finished: whether the list was read, and the key it shows now. */
  | { type: "checked"; key: string | null; read: boolean };

export function leaveReduce(state: LeaveState, event: LeaveEvent): LeaveState {
  switch (event.type) {
    case "open":
      return LEAVE_IDLE;
    case "sent":
      return { ...state, sent: true };
    case "failed":
      return {
        key: event.key,
        phase: event.failure,
        sent: true,
        checked: null,
        message: event.failure === "rejected" ? (event.message ?? null) : null,
      };
    case "checked":
      // A read that shows another revision proves the Leave sent under the old one did not
      // end the share: the state made for the old revision is over (`leaveShown`).
      if (event.key !== state.key) return { ...LEAVE_IDLE, sent: state.sent };
      return { ...state, checked: event.read ? "listed" : "unread" };
  }
}

/** The state to SHOW for the share as the list has it now. A state made for another share or
 *  another revision shows as the plain question, whatever it was. */
export function leaveShown(state: LeaveState, key: string | null): LeaveState {
  return state.key !== null && state.key === key ? state : { ...LEAVE_IDLE, sent: state.sent };
}

export const LEAVE_TAG = "Leave share";
export const LEAVE_TITLE = "Leave this share";
export const LEAVE_LABEL = "Leave";
export const LEAVE_CHECK = "Check again";

export interface LeaveDialogView {
  /** The first line. For the question it is built by the component, with the two names set
   *  in bold (`question`); every other phase is one plain sentence. */
  lead: string | null;
  question: { title: string; peer: string } | null;
  /** The explanation under it. */
  sub: string;
  /** A second line under the explanation (after Check again). */
  note: string | null;
  cancel: "Cancel" | "Close";
  confirm: typeof LEAVE_LABEL | typeof LEAVE_CHECK;
  /** What the confirm button does. `check` READS the list; it never sends Leave. */
  action: "leave" | "check";
  disabled: boolean;
  danger: boolean;
}

/** Every phase's words (the mockup's frames b, e, g and h). `peer` and `title` are text. */
export function leaveDialog(state: LeaveState, peer: string, title: string): LeaveDialogView {
  const base = { question: null, note: null, cancel: "Close", disabled: false } as const;
  switch (state.phase) {
    case "confirm":
      return {
        lead: null,
        question: { title, peer },
        sub: `The share ends on both instances now. You will no longer see this session. ${peer} is told that you left and can share it with you again.`,
        note: null,
        cancel: "Cancel",
        confirm: LEAVE_LABEL,
        action: "leave",
        disabled: false,
        danger: true,
      };
    case "offline":
      return {
        ...base,
        lead: `${peer} is not connected.`,
        sub: `Leaving ends the share on ${peer} too, so it has to be reachable. Nothing changed. Try again when it is connected. To stop everything from ${peer} at once, revoke the peer in Settings → Peers.`,
        confirm: LEAVE_LABEL,
        action: "leave",
        disabled: true,
        danger: true,
      };
    case "refused":
      return {
        ...base,
        lead: `${peer} did not accept that.`,
        sub: `The share changed on ${peer} just now (its access or end date), so it is still in your list as ${peer} has it. Leave again if you still want to end it.`,
        confirm: LEAVE_LABEL,
        action: "leave",
        danger: true,
      };
    case "rejected":
      return {
        ...base,
        lead: "That was not carried out.",
        sub: `${state.message ? `${sentence(state.message)} ` : ""}Nothing changed: the share is still in your list.`,
        confirm: LEAVE_LABEL,
        action: "leave",
        danger: true,
      };
    case "unconfirmed":
      return {
        ...base,
        lead: "We could not confirm that you left.",
        sub: `${peer} did not answer after the request was sent. The share may or may not have ended. It stays in your list until ${peer}’s own list says it is gone; nothing is sent again by itself.`,
        note:
          state.checked === "listed"
            ? `Checked: it is still in your list.`
            : state.checked === "unread"
              ? "The list could not be read. Check again in a moment."
              : null,
        confirm: LEAVE_CHECK,
        action: "check",
        danger: false,
      };
  }
}

/** A server sentence as a sentence: capitalised, closed with a full stop. */
function sentence(text: string): string {
  const t = text.trim();
  if (!t) return "";
  const head = t[0].toUpperCase() + t.slice(1);
  return /[.!?…]$/.test(head) ? head : `${head}.`;
}

/** The share's line in a Leave button's accessible name. */
export const leaveAria = (title: string, peer: string) => `Leave ${title || "this session"}, shared by ${peer}`;
