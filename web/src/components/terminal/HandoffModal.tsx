import { ArrowLeftRight, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import type { EngineInfo } from "../../types/api";
import styles from "./HandoffModal.module.css";

/** Hand-off modal (#597, Phase 1 — Quick mode): pick a target engine, review the Quick
 *  seed the server prepared, and spawn a new seeded session in that engine.
 *
 *  The engine tiles render from `/api/engines`' `supports_seed_start` — the SAME capability
 *  source the server enforces at prepare, so a disabled tile can never disagree with a
 *  server rejection. Selecting a tile re-prepares (prepare is side-effect-free; an abandoned
 *  handle just expires server-side). AI SUMMARY and seed editing are Phase 2 and render
 *  explicitly disabled. Confirm commits the handle and navigates to the normal fresh-launch
 *  route — the seed itself never travels through the URL.
 *
 *  Accessibility mirrors SessionRecapModal: dialog/aria-modal, focus in on open + back to
 *  the trigger on close, Esc + backdrop click close. */
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
  // The prepare result is keyed by the target it was built FOR: a tile switch instantly
  // invalidates the old handle/preview (derived below) without a synchronous reset here.
  const [prepRes, setPrepRes] = useState<{
    for: string;
    prep?: { handle: string; preview: string; turns: number };
    error?: string;
  } | null>(null);
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const prep = prepRes?.for === target ? (prepRes.prep ?? null) : null;
  const prepError = prepRes?.for === target ? (prepRes.error ?? null) : null;
  const preparing = target !== null && prepRes?.for !== target;
  const shownError = error ?? prepError;

  useEffect(() => {
    closeRef.current?.focus();
    return () => returnFocusTo?.focus?.();
  }, [returnFocusTo]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

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

  // (Re-)prepare whenever the target changes. Prepare is side-effect-free server-side, so
  // switching tiles just abandons the previous handle (it expires on its own TTL).
  useEffect(() => {
    if (!target) return;
    let alive = true;
    const forTarget = target;
    api
      .prepareHandoff(sessionId, forTarget)
      .then((r) => {
        if (alive)
          setPrepRes({
            for: forTarget,
            prep: { handle: r.handle, preview: r.preview, turns: r.meta.turns },
          });
      })
      .catch((e) => {
        if (alive)
          setPrepRes({
            for: forTarget,
            error: e instanceof ApiError ? e.message : "Couldn't prepare the handoff.",
          });
      });
    return () => {
      alive = false;
    };
  }, [sessionId, target]);

  const confirm = async () => {
    if (!prep || committing) return;
    setCommitting(true);
    setError(null);
    try {
      const r = await api.commitHandoff(prep.handle);
      // The normal fresh-launch route: the server redeems the seed at spawn time. Nothing
      // handoff-specific rides the URL.
      navigate(`/s/${r.engine}/${r.native}`, { state: { fresh: { cwd: r.cwd, bypass: true } } });
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Handoff failed.");
      setCommitting(false);
    }
  };

  const titleId = "handoff-modal-title";
  const kb = (n: number) => (n / 1024).toFixed(1);

  return createPortal(
    <div className={styles.backdrop} onMouseDown={onClose}>
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
            onClick={onClose}
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
                  disabled={!e.supports_seed_start}
                  title={e.seed_reason ?? undefined}
                  onClick={() => setTarget(String(e.id))}
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
          <div className={styles.tiles}>
            <button type="button" className={`${styles.tile} ${styles.tileOn}`} aria-pressed="true">
              <span className={styles.tileName}>Quick tail</span>
            </button>
            <button
              type="button"
              className={styles.tile}
              disabled
              title="AI-summarized handoff lands in Phase 2"
            >
              <span className={styles.tileName}>AI summary</span>
              <span className={styles.tileWhy}>Phase 2</span>
            </button>
          </div>
          <p className={styles.hint}>Quick tail stays local — nothing is sent to any endpoint.</p>
        </div>

        <div className={styles.section}>
          <div className={styles.previewHead}>
            <span className={styles.label}>Seed preview //</span>
            {prep && (
              <span className={styles.previewMeta}>
                LAST {prep.turns} TURNS · {kb(new Blob([prep.preview]).size)} KB
              </span>
            )}
          </div>
          {preparing ? (
            <p className={styles.muted} role="status">
              Building the handoff seed…
            </p>
          ) : prep ? (
            <textarea
              className={styles.preview}
              value={prep.preview}
              readOnly
              aria-label="Handoff seed preview (read-only — editing lands in Phase 2)"
              title="Editing the seed lands in Phase 2"
            />
          ) : (
            !shownError && <p className={styles.muted}>No preview yet.</p>
          )}
        </div>

        {shownError && (
          <p className={styles.error} role="alert">
            {shownError}
          </p>
        )}

        <div className={styles.actions}>
          <span className={styles.foot}>
            Seed is delivered to the engine as terminal input — never argv or URL.
          </span>
          <div className={styles.buttons}>
            <button type="button" className={styles.cancel} onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className={styles.go}
              onClick={confirm}
              disabled={!prep || preparing || committing}
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
