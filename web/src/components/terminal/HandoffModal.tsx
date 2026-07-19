import { ArrowLeftRight, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import type { EngineInfo, HandoffMode } from "../../types/api";
import styles from "./HandoffModal.module.css";

/** Hand-off modal (#597, Phases 1–2): pick a target engine and a seed mode, review/edit
 *  the seed the server prepared, and spawn a new seeded session in that engine.
 *
 *  The engine tiles render from `/api/engines`' `supports_seed_start` — the SAME capability
 *  source the server enforces at prepare, so a disabled tile can never disagree with a
 *  server rejection. Selecting a tile or a mode re-prepares (prepare is side-effect-free;
 *  an abandoned handle just expires server-side). AI mode degrades to the quick tail
 *  server-side when the endpoint is unconfigured/failing — `meta.notice` says so.
 *  Confirm commits the (possibly edited) seed and navigates to the normal fresh-launch
 *  route — the seed itself never travels through the URL.
 *
 *  Accessibility mirrors SessionRecapModal: dialog/aria-modal, focus in on open + back to
 *  the trigger on close, Esc + backdrop click close. Every dismissal path is disabled while
 *  a commit is in flight, so a late commit response can't redirect a user who left. */
export function HandoffModal({
  sessionId,
  engine,
  title,
  onClose,
  returnFocusTo,
}: {
  /** engine-qualified source id (`<engine>:<native_id>`). */
  sessionId: string;
  engine: string;
  /** Resolved display title of the source session (for the FROM line). */
  title: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const navigate = useNavigate();
  const [tiles, setTiles] = useState<EngineInfo[] | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [mode, setMode] = useState<HandoffMode>("quick");
  // Forces a re-prepare of the SAME target+mode (expired-handle recovery).
  const [nonce, setNonce] = useState(0);
  // The prepare result is keyed by what it was built FOR: a tile/mode switch instantly
  // invalidates the old handle/preview (derived below) without a synchronous reset here.
  const [prepRes, setPrepRes] = useState<{
    for: string;
    prep?: { handle: string; preview: string; turns: number; cap: number; notice?: string };
    error?: string;
  } | null>(null);
  // The user's edit, keyed by target|mode — deliberately NOT by the nonce, so renewing an
  // expired handle (which only bumps the nonce) PRESERVES a brief the user wrote, while a
  // genuine target/mode switch — which asks for a different seed — drops it by derivation
  // (behind a confirm, below). Hermes on #703: losing typed prose to a silent re-prepare
  // is data loss, not a refresh.
  const [editState, setEditState] = useState<{ for: string; text: string } | null>(null);
  // A target/mode switch the user must confirm because it would discard a dirty edit.
  const [pendingSwitch, setPendingSwitch] = useState<
    { target: string; mode: HandoffMode } | null
  >(null);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // An INFO line explaining why a fresh seed is being prepared (expired handle) — kept
  // SEPARATE from `error` so a failure of that re-prepare surfaces the authoritative
  // `prepError` instead of being masked by a reassuring message (#703 review follow-up).
  const [renewalNotice, setRenewalNotice] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const keepEditingRef = useRef<HTMLButtonElement>(null);
  // Guards the commit continuation against a modal unmount mid-request (browser Back /
  // external route change): a late `navigate()` must not override the user's newer
  // location (Hermes on #703 review 2586). Flipped false on unmount.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const prepKey = target ? `${target}|${mode}|${nonce}` : "";
  const editKey = target ? `${target}|${mode}` : "";
  const prep = prepRes?.for === prepKey ? (prepRes.prep ?? null) : null;
  const prepError = prepRes?.for === prepKey ? (prepRes.error ?? null) : null;
  const preparing = target !== null && prepRes?.for !== prepKey;
  const shownError = error ?? prepError;
  const edited = editState?.for === editKey ? editState.text : null;
  const seedText = edited ?? prep?.preview ?? "";
  // A dirty edit is any stored, non-empty edit that isn't identical to a loaded preview.
  // It must NOT depend on `prep` being present (Hermes on #703 round 4): during an
  // expired-handle renewal `prep` is momentarily null while `editState` still holds the
  // user's authoritative prose, and gating on `prep !== null` there dropped the discard
  // guard — a switch mid-renewal then silently discarded the brief. `prep === null` (a
  // renewal) can't prove equality, so it counts as dirty; a matched preview does not.
  const dirty =
    edited !== null && edited.trim() !== "" && (prep === null || edited !== prep.preview);
  // The cap is the SERVER's number (meta.cap) — the same one it enforces at commit, so the
  // UI can never invite an edit the server would reject (Hermes on #703: it used to accept
  // an over-cap brief and silently truncate it).
  const seedBytes = new Blob([seedText]).size;
  const cap = prep?.cap ?? 0;
  const overCap = cap > 0 && seedBytes > cap;

  // Dismissal is disabled while committing: the commit response navigates, so letting a
  // user close/cancel mid-flight would redirect them after they left (Hermes on #701).
  // A pending discard decision takes precedence over closing the whole modal: Escape /
  // backdrop cancel the "Discard your edits?" prompt FIRST, so neither bypasses it and
  // silently drops the edited brief (Hermes on #703 review 2586).
  const dismiss = useCallback(() => {
    if (committing) return;
    if (pendingSwitch) {
      setPendingSwitch(null);
      return;
    }
    onClose();
  }, [committing, pendingSwitch, onClose]);

  useEffect(() => {
    closeRef.current?.focus();
    return () => returnFocusTo?.focus?.();
  }, [returnFocusTo]);

  // Move focus INTO the discard-confirm when it opens (onto the safe "Keep editing"
  // default), so it reads as a real decision point rather than a passive banner — and
  // Escape/backdrop cancel it rather than the parent (via `dismiss`) — Hermes review 2586.
  const hasPendingSwitch = pendingSwitch !== null;
  useEffect(() => {
    if (hasPendingSwitch) keepEditingRef.current?.focus();
  }, [hasPendingSwitch]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        dismiss();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dismiss]);

  // Engine tiles from the server capability source. Default target: the first seed-capable
  // engine that isn't the source (same-engine handoff is allowed, just not the default).
  useEffect(() => {
    let alive = true;
    api
      .engines()
      .then((r) => {
        if (!alive) return;
        const list = r.engines.filter((e) => e.id !== "shell");
        setTiles(list);
        const enabled = list.filter((e) => e.supports_seed_start);
        const def = enabled.find((e) => e.id !== engine) ?? enabled[0];
        if (def) setTarget(String(def.id));
        else setError("No engine on this host can accept a handoff yet.");
      })
      .catch(() => alive && setError("Couldn't load the engine list."));
    return () => {
      alive = false;
    };
  }, [engine]);

  // (Re-)prepare whenever the target, the mode, or the retry nonce changes. Prepare is
  // side-effect-free server-side, so switching just abandons the previous handle (it
  // expires on its own TTL). Any edit belonged to the old seed and is dropped by the
  // key derivation above — no reset needed here.
  useEffect(() => {
    if (!target) return;
    let alive = true;
    const forKey = `${target}|${mode}|${nonce}`;
    api
      .prepareHandoff(sessionId, target, mode)
      .then((r) => {
        if (alive)
          setPrepRes({
            for: forKey,
            prep: {
              handle: r.handle,
              preview: r.preview,
              turns: r.meta.turns,
              cap: r.meta.cap,
              notice: r.meta.notice,
            },
          });
      })
      .catch((e) => {
        if (alive)
          setPrepRes({
            for: forKey,
            error: e instanceof ApiError ? e.message : "Couldn't prepare the handoff.",
          });
      });
    return () => {
      alive = false;
    };
  }, [sessionId, target, mode, nonce]);

  // A switch that would throw away typed prose asks first; a clean preview switches
  // immediately (nothing to lose).
  const requestSwitch = (next: { target?: string; mode?: HandoffMode }) => {
    const t = next.target ?? target ?? "";
    const m = next.mode ?? mode;
    if (t === target && m === mode) return;
    if (dirty) {
      setPendingSwitch({ target: t, mode: m });
      return;
    }
    setError(null);
    setRenewalNotice(null);
    setTarget(t);
    setMode(m);
  };

  const applyPendingSwitch = () => {
    if (!pendingSwitch || committing) return;
    setEditState(null); // the edit belonged to the seed we're leaving
    setError(null);
    setRenewalNotice(null);
    setTarget(pendingSwitch.target);
    setMode(pendingSwitch.mode);
    setPendingSwitch(null);
  };

  const confirm = async () => {
    // A pending discard decision suspends the action entirely: the handle/target on screen
    // is the one the user may be about to abandon, and committing it mid-decision would
    // navigate them somewhere they didn't choose (#703 review round 3).
    if (!prep || committing || overCap || pendingSwitch) return;
    setCommitting(true);
    setError(null);
    setRenewalNotice(null);
    try {
      const r = await api.commitHandoff(prep.handle, edited ?? undefined);
      // If the modal unmounted while the request was in flight (browser Back, external
      // route change), the user has already navigated — this stale continuation must NOT
      // yank them to the new session (Hermes on #703 review 2586). The committed handoff
      // is still valid server-side; it just isn't this dead modal's job to route to it.
      if (!mountedRef.current) return;
      // The normal fresh-launch route: the server redeems the seed at spawn time. Nothing
      // handoff-specific rides the URL.
      navigate(`/s/${r.engine}/${r.native}`, { state: { fresh: { cwd: r.cwd, bypass: true } } });
      onClose();
    } catch (e) {
      if (!mountedRef.current) return; // unmounted mid-request — nothing to surface
      // A handle the server won't honour (404 expired/consumed/vanished, 409 already
      // committed) is recoverable in place: re-prepare a fresh handle and let the user
      // confirm again. Round-4 follow-up: the "re-prepared" text is a NOTICE, not an
      // `error` — setting `error` masked the re-prepare's own failure (shownError =
      // error ?? prepError) and dead-ended the modal. Clearing `error` lets `prepError`
      // surface if the fresh prepare fails, and the Retry affordance (below) recovers it.
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        setRenewalNotice(
          "That handoff could no longer be committed — a fresh seed is being prepared. " +
            "Review it and hand off again.",
        );
        setNonce((n) => n + 1);
      } else {
        setError(e instanceof ApiError ? e.message : "Handoff failed.");
      }
      setCommitting(false);
    }
  };

  // Retry the SAME target+mode after a prepare failure (including a failed renewal) — the
  // one recovery path the modal lacked (#703 review follow-up): a prepare error previously
  // dead-ended unless the user switched target/mode.
  const retryPrepare = () => {
    setError(null);
    setRenewalNotice(null);
    setNonce((n) => n + 1);
  };

  const titleId = "handoff-modal-title";
  const kb = (bytes: number) => (bytes / 1024).toFixed(1);

  return createPortal(
    <div className={styles.backdrop} onMouseDown={dismiss}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.dialog}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={styles.head}>
          <span className={styles.tag} id={titleId}>
            Hand off // session
          </span>
          <button
            ref={closeRef}
            type="button"
            className={styles.close}
            onClick={dismiss}
            disabled={committing}
            aria-label="Close hand-off dialog"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <p className={styles.from}>
          <span className={styles.fromLabel}>FROM //</span>
          <b className={styles.fromTitle}>
            {engine.toUpperCase()} · {title}
          </b>
        </p>

        <div className={styles.section}>
          <span className={styles.label}>Target engine //</span>
          {tiles === null && !error ? (
            <p className={styles.muted}>Loading engines…</p>
          ) : (
            <div className={styles.tiles} role="radiogroup" aria-label="Target engine">
              {(tiles ?? []).map((e) => (
                <button
                  key={String(e.id)}
                  type="button"
                  role="radio"
                  aria-checked={target === e.id}
                  className={`${styles.tile} ${target === e.id ? styles.tileOn : ""}`}
                  disabled={!e.supports_seed_start || committing}
                  title={e.seed_reason ?? undefined}
                  onClick={() => requestSwitch({ target: String(e.id) })}
                >
                  <span className={styles.tileName}>{String(e.id).toUpperCase()}</span>
                  {e.seed_reason && <span className={styles.tileWhy}>{e.seed_reason}</span>}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className={styles.section}>
          <span className={styles.label}>Seed mode //</span>
          <div className={styles.tiles} role="radiogroup" aria-label="Seed mode">
            <button
              type="button"
              role="radio"
              aria-checked={mode === "quick"}
              className={`${styles.tile} ${mode === "quick" ? styles.tileOn : ""}`}
              disabled={committing}
              onClick={() => requestSwitch({ mode: "quick" })}
            >
              <span className={styles.tileName}>Quick tail</span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={mode === "ai"}
              className={`${styles.tile} ${mode === "ai" ? styles.tileOn : ""}`}
              disabled={committing}
              onClick={() => requestSwitch({ mode: "ai" })}
            >
              <span className={styles.tileName}>AI summary</span>
            </button>
          </div>
          <p className={styles.hint}>
            {mode === "ai"
              ? "AI summary sends this session's transcript tail to your configured AI-review endpoint to build the brief."
              : "Quick tail builds the seed locally — no AI endpoint is called."}
          </p>
        </div>

        <div className={styles.section}>
          <div className={styles.previewHead}>
            <span className={styles.label}>Seed preview // editable</span>
            {prep && (
              <span className={`${styles.previewMeta} ${overCap ? styles.previewOver : ""}`}>
                {prep.turns} TURNS · {kb(seedBytes)} / {kb(cap)} KB
              </span>
            )}
          </div>
          {/* Keep the editor mounted whenever there is text to show — a loaded preview OR a
              pending edit being carried through an expired-handle renewal (#703 round 4).
              The "building" state is only for the INITIAL prepare, when there is nothing to
              preserve yet; a renewal keeps the user's prose visible and editable. */}
          {prep || edited !== null ? (
            <textarea
              className={styles.preview}
              value={seedText}
              onChange={(e) => setEditState({ for: editKey, text: e.target.value })}
              disabled={committing}
              aria-label="Handoff seed preview"
              spellCheck={false}
            />
          ) : preparing ? (
            <p className={styles.muted} role="status">
              {mode === "ai" ? "Summarizing this session…" : "Building the handoff seed…"}
            </p>
          ) : (
            !shownError && <p className={styles.muted}>No preview yet.</p>
          )}
          {preparing && (prep || edited !== null) && (
            <p className={styles.muted} role="status">
              Preparing a fresh handoff…
            </p>
          )}
          {prep?.notice && (
            <p className={styles.notice} role="status">
              {prep.notice}
            </p>
          )}
          {/* Renewal notice — shown only while no real error is up, so a failed re-prepare
              surfaces its own error instead of this reassuring line (#703 review follow-up). */}
          {renewalNotice && !shownError && (
            <p className={styles.notice} role="status">
              {renewalNotice}
            </p>
          )}
          {overCap && (
            <p className={styles.error} role="alert">
              This brief is {kb(seedBytes)} KB — over the {kb(cap)} KB limit the target
              accepts. Trim it to hand off.
            </p>
          )}
        </div>

        {shownError && (
          <p className={styles.error} role="alert">
            {shownError}
            {/* A prepare failure (including a failed renewal) can be retried in place —
                the recovery path the modal previously lacked (#703 review follow-up). */}
            {prepError && !committing && (
              <button type="button" className={styles.retry} onClick={retryPrepare}>
                Retry
              </button>
            )}
          </p>
        )}

        {pendingSwitch && (
          <div
            className={styles.confirm}
            role="alertdialog"
            aria-modal="true"
            aria-label="Discard your edits?"
          >
            <span>Switching rebuilds the seed and discards your edited brief.</span>
            <div className={styles.confirmBtns}>
              <button
                ref={keepEditingRef}
                type="button"
                className={styles.cancel}
                onClick={() => setPendingSwitch(null)}
                disabled={committing}
              >
                Keep editing
              </button>
              <button
                type="button"
                className={styles.discard}
                onClick={applyPendingSwitch}
                disabled={committing}
              >
                Discard &amp; rebuild
              </button>
            </div>
          </div>
        )}

        <div className={styles.actions}>
          <span className={styles.foot}>
            The seed is pasted into the new session as its first prompt — that engine's model
            provider sees it.
          </span>
          <div className={styles.buttons}>
            <button
              type="button"
              className={styles.cancel}
              onClick={dismiss}
              disabled={committing}
            >
              Cancel
            </button>
            <button
              type="button"
              className={styles.go}
              onClick={confirm}
              disabled={
                !prep ||
                preparing ||
                committing ||
                !seedText.trim() ||
                overCap ||
                pendingSwitch !== null
              }
            >
              <ArrowLeftRight size={13} aria-hidden="true" />
              {committing ? "Handing off…" : "Hand off"}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
