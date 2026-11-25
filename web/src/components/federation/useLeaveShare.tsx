import { useCallback, useEffect, useReducer, useRef, useState, type ReactNode } from "react";
import {
  proveLeft,
  refreshRemoteShares,
  remoteSharesNow,
  useRemoteShares,
  useRemoteSharesStatus,
} from "../../app/remoteShares";
import { ApiError, api } from "../../lib/api";
import type { RemoteShare } from "../../lib/federation";
import {
  LEAVE_IDLE,
  LEAVE_TAG,
  LEAVE_TITLE,
  leaveDialog,
  leaveFailure,
  leaveKey,
  leaveReduce,
  leaveShown,
} from "../../lib/federationLeave";
import { ConfirmDialog } from "../templates/ConfirmDialog";
import { useAnswerFence } from "./useAnswerFence";

/** Which share a dialog is open for. The names are what the row showed when it was opened. */
interface Target {
  peer_id: string;
  grant_id: string;
  title: string;
  peer: string;
  opener: HTMLElement | null;
}

const find = (rows: readonly RemoteShare[], t: { peer_id: string; grant_id: string }) =>
  rows.find((r) => r.peer_id === t.peer_id && r.grant_id === t.grant_id) ?? null;

/** THE Leave action (#2201), for every place that offers it: the shared session's own header
 *  (`RemotePane`, `RemoteScreenPane`) and *Shared by <peer>* in Settings → Peers (`PeerShares`).
 *  One confirm dialog (the app's `ConfirmDialog`, focus on Cancel), one request per press.
 *
 *  - **Only the owner's `ok` removes the row** (`proveLeft`). Then `onLeft` runs: the session's
 *    own view goes to the session list.
 *  - **A refusal, an offline peer and an uncertain answer keep the row.** After a refusal or an
 *    uncertain answer the LIST is read again, never the write; *Check again* is that read too.
 *    If a successful read no longer has the share, the dialog closes as after a proven leave.
 *  - **The state is for one `(peer, grant, revision)`** (`leaveShown`): another share, a reopened
 *    dialog, or the same share at a new revision starts clean. Leave always sends the revision
 *    the list shows at the press.
 *  - **Order.** A Leave's answer never writes the list (only `ok` takes one row off, at once; the server keeps it off);
 *    list reads are ordered by the store (the newest read wins); an answer is applied to the
 *    dialog only while that dialog is open for the same share and it is the newest Leave, and
 *    the state it makes is bound to the revision the list shows THEN, so a later read at a
 *    newer revision replaces it and an older one cannot. */
export function useLeaveShare(
  onLeft?: () => void,
  /** Where focus goes when the button that opened the dialog left with its row. */
  fallbackFocus?: string,
): {
  /** Open the dialog for a listed share. `opener` gets the focus back. */
  open: (share: RemoteShare, opener?: HTMLElement | null) => void;
  /** The dialog, or `null`. Render it anywhere: it is portalled to the body. */
  dialog: ReactNode;
} {
  const shared = useRemoteShares();
  const { loaded } = useRemoteSharesStatus();
  const [target, setTarget] = useState<Target | null>(null);
  const [state, dispatch] = useReducer(leaveReduce, LEAVE_IDLE);
  const [busy, setBusy] = useState(false);
  // One request at a time, whatever a second press does before the next render.
  const flight = useRef(false);
  const attempt = useRef(0);
  const left = useRef(onLeft);
  useEffect(() => {
    left.current = onLeft;
  }, [onLeft]);
  const id = target ? `${target.peer_id}/${target.grant_id}` : null;
  // An answer applies only while the dialog is open for the share it was asked for.
  const ticket = useAnswerFence(id);

  const listed = target ? find(shared, target) : null;
  const shown = leaveShown(state, listed ? leaveKey(listed) : null);

  const open = useCallback((share: RemoteShare, opener?: HTMLElement | null) => {
    if (flight.current) return;
    dispatch({ type: "open" });
    setBusy(false);
    setTarget({
      peer_id: share.peer_id,
      grant_id: share.grant_id,
      title: share.title || "(untitled)",
      peer: share.peer_name || "the other instance",
      opener: opener ?? null,
    });
  }, []);
  const close = useCallback(() => setTarget(null), []);

  // A list that was READ and no longer has the share: it ended, and the dialog has nothing left
  // to ask, so it is not drawn (derived: no state is written for it). After this dialog sent a
  // Leave that is where the operator wanted to be, and `onLeft` runs as after a proven leave;
  // without one the owner ended it, and the view behind says so itself.
  const gone = target !== null && loaded && listed === null && !busy;
  const sent = state.sent;
  useEffect(() => {
    if (gone && sent) left.current?.();
  }, [gone, sent]);

  const keyNow = (t: { peer_id: string; grant_id: string }) => {
    const now = remoteSharesNow();
    const row = find(now.rows, t);
    return { key: row ? leaveKey(row) : null, absent: now.loaded && row === null };
  };

  const leave = async () => {
    if (!target || !listed || flight.current) return;
    flight.current = true;
    const current = ticket();
    const n = ++attempt.current;
    const share = { peer_id: target.peer_id, grant_id: target.grant_id };
    setBusy(true);
    dispatch({ type: "sent" });
    let failed: { status: number; code: unknown; message: string } | null = null;
    try {
      // The revision the list shows at this press: the share the operator is looking at.
      await api.federationRemoteLeave(share.peer_id, share.grant_id, listed.revision);
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      failed = {
        status: err?.status ?? 0,
        code: (err?.record as { code?: unknown } | null | undefined)?.code,
        message: err?.message ?? "",
      };
    }
    if (failed === null) {
      // Proof, whatever became of this dialog meanwhile: the row goes now, and the list is re-read.
      void proveLeft(share.peer_id, share.grant_id);
      flight.current = false;
      if (!current()) return;
      setBusy(false);
      setTarget(null);
      left.current?.();
      return;
    }
    const failure = leaveFailure(failed.status, failed.code);
    // A refusal or an unknown outcome: what the owner lists now is read, never the write again.
    if (failure === "refused" || failure === "unconfirmed") await refreshRemoteShares();
    flight.current = false;
    if (!current() || n !== attempt.current) return;
    setBusy(false);
    const now = keyNow(share);
    // The list no longer has it: the dialog is closed by that (`gone`); no outcome is shown.
    if (now.absent) return;
    dispatch({ type: "failed", key: now.key, failure, message: failed.message });
  };

  /** *Check again*: a READ of the list. Nothing in here can send Leave. */
  const check = async () => {
    if (!target || flight.current) return;
    flight.current = true;
    const current = ticket();
    const share = { peer_id: target.peer_id, grant_id: target.grant_id };
    setBusy(true);
    const read = await refreshRemoteShares();
    flight.current = false;
    if (!current()) return;
    setBusy(false);
    const now = keyNow(share);
    if (read && now.absent) return; // gone: that closes the dialog
    dispatch({ type: "checked", key: now.key, read });
  };

  let dialog: ReactNode = null;
  if (target && !gone) {
    const view = leaveDialog(shown, target.peer, target.title);
    dialog = (
      <ConfirmDialog
        tag={LEAVE_TAG}
        title={LEAVE_TITLE}
        cancelLabel={view.cancel}
        confirmLabel={busy ? (view.action === "check" ? "Checking…" : "Leaving…") : view.confirm}
        confirmDisabled={view.disabled || (view.action === "leave" && !listed)}
        danger={view.danger}
        busy={busy}
        onCancel={close}
        onConfirm={() => void (view.action === "check" ? check() : leave())}
        returnFocusTo={target.opener}
        fallbackFocusSelector={fallbackFocus}
        markers={{
          dialog: { "data-testid": "leave-dialog", "data-phase": shown.phase },
          confirm: { "data-testid": view.action === "check" ? "leave-check" : "leave-confirm" },
        }}
      >
        {view.question ? (
          <p data-testid="leave-lead">
            Stop receiving <b>{view.question.title}</b> from <b>{view.question.peer}</b>?
          </p>
        ) : (
          <p role="alert" data-testid="leave-lead">
            {view.lead}
          </p>
        )}
        <p data-testid="leave-sub">{view.sub}</p>
        {view.note && (
          <p role="status" data-testid="leave-note">
            {view.note}
          </p>
        )}
      </ConfirmDialog>
    );
  }
  return { open, dialog };
}
