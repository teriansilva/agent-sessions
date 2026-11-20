import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, api } from "../../lib/api";
import {
  ACCESS_NOTE_MAX,
  ASK_TILES,
  ASK_UNCONFIRMED,
  accessFailure,
  type AccessFailure,
  askLead,
  askLevels,
} from "../../lib/federationTogether";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import { PeerBadge } from "./PeerBadge";
import share from "./SharePeer.module.css";
import styles from "./Together.module.css";
import { useAnswerFence } from "./useAnswerFence";

/** **Ask for access** (#2062): the receiving side asks the owner's operator for a higher level.
 *
 *  Only the levels ABOVE the current one are offered. The optional note is plain text (280
 *  characters, counted), shown to the other operator as plain text and to nobody else. Sending
 *  a request changes no access: the owner's operator decides, in their own share controls.
 *  One request, one press: nothing here retries.
 *
 *  **An answer that proves nothing says nothing** (#2061's `writeFailure`). `504 peer_lost`, a
 *  timeout, `429 busy`, the network or any unlisted answer may mean the request arrived: the
 *  dialog says it could not confirm, never "sent" or "not sent", and the pane reads `access`
 *  again; when that read shows the request waiting, the pane closes this dialog. */
export function AskAccessDialog({
  peerId,
  grantId,
  owner,
  title,
  level,
  onClose,
  onAnswer,
  returnFocusTo,
}: {
  peerId: string;
  grantId: string;
  owner: string;
  title: string;
  /** The level this instance holds now. */
  level: string;
  onClose: () => void;
  /** What the owner answered: a request (`201`), or what the failure was (`accessFailure`). */
  onAnswer: (result: { ok: true } | { ok: false; failure: AccessFailure }) => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const offered = askLevels(level).filter((l): l is "suggest" | "send" => l !== "view");
  const [picked, setPicked] = useState<"suggest" | "send" | null>(null);
  const chosen = picked && offered.includes(picked) ? picked : (offered[0] ?? null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // An answer may change this dialog only while it is open for the share it was sent for. Armed
  // in an effect body, so StrictMode's replay arms it again (`useAnswerFence`, #2061).
  const ticket = useAnswerFence(`${peerId}/${grantId}`);
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const length = [...note].length;
  const send = async () => {
    if (!chosen || busy || length > ACCESS_NOTE_MAX) return;
    const current = ticket();
    setBusy(true);
    setError(null);
    try {
      const text = note.trim();
      await api.askFederationRemoteAccess(peerId, grantId, text ? { level: chosen, note: text } : { level: chosen });
      onAnswer({ ok: true });
      if (current()) onClose();
    } catch (e) {
      const failure = accessFailure(
        e instanceof ApiError ? e.status : 0,
        e instanceof ApiError ? (e.record as { code?: unknown } | null | undefined)?.code : undefined,
      );
      // The real outcome, once: nothing here sends the request again.
      onAnswer({ ok: false, failure });
      if (!current()) return;
      if (failure === "ended" || failure === "offline") return onClose(); // the pane says why
      setError(
        failure === "rejected"
          ? `${(e instanceof ApiError && e.message) || "That request was not taken."} Nothing was sent.`
          : ASK_UNCONFIRMED,
      );
      setBusy(false);
    }
  };

  const titleId = "ask-access-title";
  return createPortal(
    <div className={dlg.backdrop} onMouseDown={busy ? undefined : onClose}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${dlg.pinned} ${styles.dialog}`}
        // Opened from a menu: the menu's focus restore leaves focus here rather than on its trigger.
        data-focus-claim=""
        data-testid="ask-access"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <h2 id={titleId} className={dlg.tag}>
            Ask for access
          </h2>
          <button
            ref={closeRef}
            type="button"
            className={`${dlg.close} ${styles.close}`}
            aria-label="Close"
            disabled={busy}
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <p className={`${dlg.from} ${share.from}`}>
          <span className={dlg.fromLabel}>SESSION</span>
          <span className={dlg.fromTitle}>{title}</span>
          <PeerBadge name={owner} />
        </p>
        <div className={`${dlg.sheetBody} ${share.body}`}>
          <p className={`${styles.help} ${styles.helpFirst}`}>{askLead(owner)}</p>
          <span className={`${dlg.label} ${share.label}`} id="ask-access-level">
            Ask for
          </span>
          <div
            className={`${share.tiles} ${offered.length > 1 ? styles.tilesTwo : ""}`}
            role="radiogroup"
            aria-labelledby="ask-access-level"
          >
            {offered.map((l) => (
              <button
                key={l}
                type="button"
                role="radio"
                aria-checked={chosen === l}
                className={share.tile}
                data-level={l}
                disabled={busy}
                onClick={() => setPicked(l)}
              >
                <span className={share.tileBody}>
                  <span className={share.tileName}>{ASK_TILES[l].name}</span>
                  <span className={share.tileDesc}>{ASK_TILES[l].desc(owner)}</span>
                </span>
              </button>
            ))}
          </div>
          <label className={`${dlg.label} ${share.label}`} htmlFor="ask-access-note">
            Note <span className={styles.optional}>(optional)</span>
          </label>
          <textarea
            id="ask-access-note"
            className={styles.noteField}
            value={note}
            disabled={busy}
            autoComplete="off"
            data-testid="ask-access-note"
            onChange={(e) => setNote(e.target.value)}
          />
          <p
            className={`${styles.counter} ${length > ACCESS_NOTE_MAX ? styles.over : ""}`}
            data-testid="ask-access-count"
          >
            {length} / {ACCESS_NOTE_MAX}
          </p>
          {error && (
            <p className={dlg.error} role="alert" data-testid="ask-access-error" data-failure={error === ASK_UNCONFIRMED ? "unconfirmed" : "rejected"}>
              {error}
            </p>
          )}
        </div>
        <div className={`${dlg.actions} ${share.actions}`}>
          <button type="button" className={dlg.cancel} disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={dlg.go}
            disabled={!chosen || busy || length > ACCESS_NOTE_MAX}
            data-testid="ask-access-go"
            onClick={() => void send()}
          >
            {busy ? "Sending…" : "Send request"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
