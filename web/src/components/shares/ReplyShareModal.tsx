import { Copy, Link2, Share2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import {
  REPLY_EXPIRY,
  formatWhen,
  replyLink,
  type CreatedReplyShare,
  type ReplyExpiry,
} from "../../lib/shareFormat";
import { settingsPath } from "../../routes/settingsTabs";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import styles from "./Shares.module.css";

/** What a visitor of a reply link sees (#1956), said before the link exists. */
export const REPLY_DISCLOSURE =
  "Anyone with the link can read this reply only, without an account. Commands and tool output" +
  " are not included, only how many there were; saved secret values are hidden. Read only.";

export const REPLY_SHOWN_ONCE =
  "The link holds its own key and is shown once: this BattleLab keeps only the sealed copy." +
  " Copy it now; to share again, create a new link.";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** **Share this reply** (#1956): a read-only link to ONE finished agent reply of an API session,
 *  with an optional expiry and password. The server seals a redacted copy of the reply (tools
 *  reduced to a count) under a key that exists only in the link's fragment, so the link is shown
 *  exactly once, here. It opens on whatever domain this page is on. */
export function ReplyShareModal({
  sessionKey,
  turnId,
  agent,
  onClose,
  returnFocusTo,
}: {
  sessionKey: string;
  turnId: string;
  agent: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const urlRef = useRef<HTMLInputElement | null>(null);
  const [expiry, setExpiry] = useState<ReplyExpiry>("7d");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedReplyShare | null>(null);
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";
  // The create's answer is the link's only copy: the dialog can't be dismissed while it is in
  // flight (the same rule as Share publicly, #1444 review).
  const creatingRef = useRef(false);
  const close = useCallback(() => {
    if (!creatingRef.current) onClose();
  }, [onClose]);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [close]);
  useEffect(() => {
    if (created) urlRef.current?.focus({ preventScroll: true });
  }, [created]);

  const url = created ? replyLink(created.path, created.key) : "";

  const create = async () => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setError(null);
    try {
      const made = await api.createReplyShare({
        session: sessionKey,
        turn_id: turnId,
        expires_in: expiry,
        ...(password ? { password } : {}),
      });
      setCreated(made);
      setPassword("");
      setCopied(null);
    } catch (e) {
      setError(message(e, "Couldn’t create the link."));
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied("ok");
    } catch {
      urlRef.current?.select();
      setCopied("failed");
    }
  };

  const titleId = "reply-share-title";
  return createPortal(
    <div className={dlg.backdrop} onMouseDown={created ? undefined : close}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${styles.dialog}`}
        data-testid="reply-share"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Share this reply
          </span>
          <button
            ref={closeRef}
            type="button"
            className={`${dlg.close} ${styles.close}`}
            aria-label="Close"
            disabled={creating}
            onClick={close}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <p className={dlg.from}>
          <span className={dlg.fromLabel}>REPLY</span>
          <span className={styles.badge}>{agent}</span>
        </p>
        <p className={styles.disclosure} data-testid="reply-share-disclosure">
          {REPLY_DISCLOSURE}
        </p>

        {created ? (
          <section aria-label="New reply link" data-testid="reply-share-created">
            <div className={styles.chips}>
              <span className={`${styles.chip} ${styles.chipMode}`}>READ ONLY</span>
              <span
                className={created.share.has_password ? styles.chip : `${styles.chip} ${styles.chipWarn}`}
              >
                {created.share.has_password ? "PASSWORD" : "NO PASSWORD"}
              </span>
              <span className={styles.chip}>
                {created.share.expires_at == null
                  ? "NEVER EXPIRES"
                  : `ENDS ${formatWhen(created.share.expires_at).toUpperCase()}`}
              </span>
            </div>
            <label className={dlg.label} htmlFor="reply-share-url">
              Reply link — shown once
            </label>
            <input
              ref={urlRef}
              id="reply-share-url"
              className={`${dlg.input} ${styles.url}`}
              readOnly
              value={url}
              data-testid="reply-share-url"
              onFocus={(e) => e.currentTarget.select()}
            />
            <p className={styles.help}>{REPLY_SHOWN_ONCE}</p>
            <div className={styles.linkActions}>
              <button
                type="button"
                className={canShare ? dlg.cancel : dlg.go}
                onClick={() => void copy()}
              >
                <Copy size={14} aria-hidden="true" /> {copied === "ok" ? "Copied" : "Copy"}
              </button>
              {canShare && (
                <button
                  type="button"
                  className={dlg.go}
                  onClick={() => void navigator.share({ title: "Shared reply", url }).catch(() => {})}
                >
                  <Share2 size={14} aria-hidden="true" /> Share…
                </button>
              )}
            </div>
            {copied === "failed" && (
              <p className={dlg.error} role="alert">
                Couldn’t copy here — the link is selected, copy it from the field.
              </p>
            )}
            <p className={styles.help}>
              Revoke it any time in{" "}
              <Link className={styles.inlineLink} to={settingsPath("sharing")} onClick={onClose}>
                Settings → Sharing
              </Link>
              .
            </p>
          </section>
        ) : (
          <section aria-label="New reply link">
            <span className={dlg.label} id="reply-share-expiry">
              Expires
            </span>
            <div className={styles.presets} role="group" aria-labelledby="reply-share-expiry">
              {REPLY_EXPIRY.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  aria-pressed={expiry === p.id}
                  className={styles.preset}
                  onClick={() => setExpiry(p.id)}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <label className={dlg.label} htmlFor="reply-share-password">
              Password <span className={styles.optional}>(optional)</span>
            </label>
            <div className={styles.pwRow}>
              <input
                id="reply-share-password"
                className={dlg.input}
                type={showPw ? "text" : "password"}
                autoComplete="new-password"
                value={password}
                maxLength={256}
                onChange={(e) => setPassword(e.target.value)}
              />
              <button
                type="button"
                className={`${dlg.cancel} ${styles.show}`}
                aria-pressed={showPw}
                onClick={() => setShowPw((v) => !v)}
              >
                {showPw ? "Hide" : "Show"}
              </button>
            </div>
            <p className={styles.help}>
              A visitor types the password in their browser; it never reaches this BattleLab.
            </p>
            {error && (
              <p className={dlg.error} role="alert">
                {error}
              </p>
            )}
            <div className={dlg.actions}>
              <button type="button" className={dlg.cancel} disabled={creating} onClick={close}>
                Cancel
              </button>
              <button
                type="button"
                className={dlg.go}
                disabled={creating}
                onClick={() => void create()}
              >
                <Link2 size={14} aria-hidden="true" /> {creating ? "Creating…" : "Create link"}
              </button>
            </div>
          </section>
        )}
      </div>
    </div>,
    document.body,
  );
}
