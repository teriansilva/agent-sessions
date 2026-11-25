import { Copy, Globe, Share2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import {
  EXPIRY_PRESETS,
  expiryFor,
  formatWhen,
  nowSeconds,
  type CreatedShare,
  type ExpiryPreset,
  type ShareList,
  type ShareMode,
} from "../../lib/shareFormat";
import { settingsPath } from "../../routes/settingsTabs";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import { ShareRow } from "./ShareRow";
import styles from "./Shares.module.css";

/** The send-link risk, word for word as the server states it (`routes/shares.SEND_RISK`, the
 *  operator's decision on #1423). Shown BEFORE the link is made: creating a `send` link sends
 *  `accept_risk: true`, and the server refuses one without it. `shareCopy.test.ts` holds this to
 *  the server's text. */
export const SEND_RISK =
  "Anyone with this link can instruct the agent with this session's permissions, including" +
  " reaching saved secret values the agent can see.";

/** What a visitor of an API session sees (#1404's disclosure table, mockup A1 r3). */
export const API_DISCLOSURE =
  "Visitors see the conversation and its attached images. Only saved secret values are hidden;" +
  " anything else typed or printed in it is visible.";

export const SHOWN_ONCE =
  "Only a fingerprint of the link is stored. Copy it now; to share again, create a new link.";

function message(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** **Share publicly…** (#1404 P4): make a link anyone can open without a BattleLab account, and
 *  see / revoke this session's live links.
 *
 *  API sessions only for now: the terminal viewer needs the read-only terminal stream (P3), so a
 *  terminal session does not offer this yet rather than mint a link that would show nothing.
 *
 *  The link is shown ONCE — the server keeps only its digest — so the created state leads with it,
 *  the risk (send links) and what visitors see, all above the fold of the dialog. */
export function SharePubliclyModal({
  sessionKey,
  title,
  agent,
  onClose,
  returnFocusTo,
}: {
  sessionKey: string;
  title: string;
  /** The agent's label, for the badge beside the title. */
  agent: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const urlRef = useRef<HTMLInputElement | null>(null);
  const [list, setList] = useState<ShareList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [mode, setMode] = useState<ShareMode>("view");
  // What visitors see as the link's title. It starts as the session's, which can fall back to
  // its first message, so the operator sees it and can change it (the server redacts saved
  // secret values in it too).
  const [publicTitle, setPublicTitle] = useState(title);
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [preset, setPreset] = useState<ExpiryPreset>("1d");
  const [picked, setPicked] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<CreatedShare | null>(null);
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  // Re-render the relative times (expiry hint, "opened … ago") without a clock subscription.
  const [now, setNow] = useState(nowSeconds);
  const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";
  // While the create is in flight the dialog can't be dismissed: its answer carries the link's
  // ONLY copy, and a dialog closed before it lands would leave a live link nobody can copy
  // (#1444 review). A ref, so the Escape listener sees it without re-subscribing.
  const creatingRef = useRef(false);
  const close = useCallback(() => {
    if (!creatingRef.current) onClose();
  }, [onClose]);

  const reload = useCallback(() => {
    return api
      .shares(sessionKey)
      .then((r) => {
        setList(r);
        setListError(null);
        setNow(nowSeconds());
      })
      .catch((e: unknown) => setListError(message(e, "Couldn’t load this session’s links.")));
  }, [sessionKey]);

  useEffect(() => {
    void reload();
  }, [reload]);

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
  // The new link leads the dialog: back to its top (the Create click may have scrolled it), then
  // focus the link so it can be copied straight away.
  useEffect(() => {
    if (!created) return;
    if (dialogRef.current) dialogRef.current.scrollTop = 0;
    urlRef.current?.focus({ preventScroll: true });
  }, [created]);

  const expiresAt = expiryFor(preset, now, picked);
  const badDate = typeof expiresAt === "number" && !Number.isFinite(expiresAt);
  const available = list?.available !== false;

  const create = async () => {
    if (creatingRef.current || badDate || !available) return;
    creatingRef.current = true;
    setCreating(true);
    setError(null);
    try {
      const made = await api.createShare({
        session: sessionKey,
        mode,
        title: publicTitle.trim() || "Shared session",
        ...(password ? { password } : {}),
        expires_at: expiryFor(preset, nowSeconds(), picked),
        ...(mode === "send" ? { accept_risk: true as const } : {}),
      });
      setCreated(made);
      setPassword("");
      setCopied(null);
      await reload();
    } catch (e) {
      setError(message(e, "Couldn’t create the link."));
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  };

  const revoke = async (shareId: string) => {
    setRevoking(shareId);
    setError(null);
    try {
      await api.revokeShare(shareId);
      // The link on screen no longer opens anything: don't keep offering it for copying.
      if (created?.share.share_id === shareId) setCreated(null);
    } catch (e) {
      setError(message(e, "Couldn’t revoke that link."));
    } finally {
      setRevoking(null);
      await reload();
    }
  };

  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.url);
      setCopied("ok");
    } catch {
      urlRef.current?.select();
      setCopied("failed");
    }
  };

  const shareSheet = async () => {
    if (!created) return;
    try {
      await navigator.share({ title, url: created.url });
    } catch {
      /* dismissed, or the platform refused: the link is still on screen to copy */
    }
  };

  const titleId = "share-publicly-title";
  const live = list?.shares ?? [];
  const createdMode = created?.share.mode;

  return createPortal(
    // Once a link exists it is on screen for the last time: a stray click outside must not
    // close it (Close and Escape still do).
    <div className={dlg.backdrop} onMouseDown={created ? undefined : close}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${styles.dialog}`}
        data-testid="share-publicly"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            Share publicly
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
          <span className={dlg.fromLabel}>SESSION</span>
          <span className={dlg.fromTitle}>{title}</span>
          <span className={styles.badge}>{agent}</span>
        </p>

        {created ? (
          <section aria-label="New public link" data-testid="share-created">
            <div className={styles.chips}>
              <span className={`${styles.chip} ${styles.chipMode}`}>
                {createdMode === "send" ? "CAN SEND MESSAGES" : "VIEW ONLY"}
              </span>
              <span className={created.share.has_password ? styles.chip : `${styles.chip} ${styles.chipWarn}`}>
                {created.share.has_password ? "PASSWORD" : "NO PASSWORD"}
              </span>
              <span className={styles.chip}>
                {created.share.expires_at == null
                  ? "NEVER EXPIRES"
                  : `ENDS ${formatWhen(created.share.expires_at).toUpperCase()}`}
              </span>
            </div>
            {createdMode === "send" && (
              <p className={styles.risk} data-testid="share-risk">
                <b>RISKY</b> · {created.risk ?? SEND_RISK} Approvals stay with you.
              </p>
            )}
            <p className={styles.disclosure} data-testid="share-disclosure">
              {API_DISCLOSURE}
            </p>
            <label className={dlg.label} htmlFor="share-url">
              Public link — shown once
            </label>
            <input
              ref={urlRef}
              id="share-url"
              className={`${dlg.input} ${styles.url}`}
              readOnly
              value={created.url}
              data-testid="share-url"
              onFocus={(e) => e.currentTarget.select()}
            />
            <p className={styles.help} data-testid="share-once">
              {SHOWN_ONCE}
            </p>
            <div className={styles.linkActions}>
              <button type="button" className={canShare ? dlg.cancel : dlg.go} onClick={() => void copy()}>
                <Copy size={14} aria-hidden="true" /> {copied === "ok" ? "Copied" : "Copy"}
              </button>
              {canShare && (
                <button type="button" className={dlg.go} onClick={() => void shareSheet()}>
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
              Manage all public links in{" "}
              <Link className={styles.inlineLink} to={settingsPath("sharing")} onClick={onClose}>
                Settings → Sharing
              </Link>
              .
            </p>
          </section>
        ) : (
          <section aria-label="New public link">
            {!available && (
              <p className={dlg.error} role="note" data-testid="share-unavailable">
                {list?.reason ?? "Public links are off on this BattleLab."}
              </p>
            )}
            <span className={dlg.label} id="share-access">
              Access
            </span>
            <div className={styles.tiles} role="radiogroup" aria-labelledby="share-access">
              {(
                [
                  ["view", "View only", "Watch the conversation live"],
                  ["send", "Can send messages", "Send turns, attach images, interrupt"],
                ] as const
              ).map(([id, name, desc]) => (
                <button
                  key={id}
                  type="button"
                  role="radio"
                  aria-checked={mode === id}
                  className={styles.tile}
                  onClick={() => setMode(id)}
                >
                  <span className={styles.tileName}>{name}</span>
                  <span className={styles.tileDesc}>{desc}</span>
                </button>
              ))}
            </div>
            {mode === "send" && (
              <p className={styles.risk} data-testid="share-risk">
                <b>RISKY</b> · {SEND_RISK} Approvals stay with you.
              </p>
            )}
            <p className={styles.disclosure} data-testid="share-disclosure">
              {API_DISCLOSURE}
            </p>
            <label className={dlg.label} htmlFor="share-title">
              Title visitors see
            </label>
            <input
              id="share-title"
              className={`${dlg.input} ${styles.field}`}
              value={publicTitle}
              maxLength={200}
              onChange={(e) => setPublicTitle(e.target.value)}
            />
            <label className={dlg.label} htmlFor="share-password">
              Password <span className={styles.optional}>(optional)</span>
            </label>
            <div className={styles.pwRow}>
              <input
                id="share-password"
                className={dlg.input}
                type={showPw ? "text" : "password"}
                autoComplete="new-password"
                value={password}
                maxLength={200}
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
            <span className={dlg.label} id="share-expiry">
              Expires <span className={styles.optional}>(optional)</span>
            </span>
            <div className={styles.presets} role="group" aria-labelledby="share-expiry">
              {EXPIRY_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  aria-pressed={preset === p.id}
                  className={styles.preset}
                  onClick={() => {
                    setPreset(p.id);
                    setNow(nowSeconds());
                  }}
                >
                  {p.label}
                </button>
              ))}
            </div>
            {preset === "date" && (
              <input
                className={`${dlg.input} ${styles.date}`}
                type="datetime-local"
                aria-label="Link ends at"
                value={picked}
                onChange={(e) => setPicked(e.target.value)}
              />
            )}
            <p className={styles.help} data-testid="share-expiry-hint">
              {expiresAt == null
                ? "Never expires. You can revoke it at any time."
                : badDate
                  ? "Pick the date and time the link should stop working."
                  : `Ends ${formatWhen(expiresAt)}. You can revoke it before then.`}
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
                disabled={creating || badDate || !available || !list}
                onClick={() => void create()}
              >
                <Globe size={14} aria-hidden="true" /> {creating ? "Creating…" : "Create public link"}
              </button>
            </div>
          </section>
        )}

        <section className={styles.listSection} aria-labelledby="share-live-h">
          <h3 id="share-live-h" className={dlg.label}>
            Public links to this session · {live.length}
          </h3>
          {listError && (
            <p className={dlg.error} role="alert">
              {listError}
            </p>
          )}
          {created && error && (
            <p className={dlg.error} role="alert">
              {error}
            </p>
          )}
          {list && live.length === 0 && <p className={dlg.muted}>No active links to this session.</p>}
          {live.length > 0 && (
            <ul className={styles.rows}>
              {live.map((s) => (
                <ShareRow
                  key={s.share_id}
                  share={s}
                  now={now}
                  busy={revoking === s.share_id}
                  onRevoke={() => void revoke(s.share_id)}
                />
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>,
    document.body,
  );
}
