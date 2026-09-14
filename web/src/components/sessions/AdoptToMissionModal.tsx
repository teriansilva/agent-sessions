import { Crosshair, Search } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { engineBadge, relTime } from "../../lib/format";
import { announceSessionMissionChanged } from "../../lib/missionEvents";
import { MISSION_PATH } from "../../lib/missionLink";
import type { MissionListRow, Session, SessionMissionRef } from "../../types/api";
import { useFocusContainment } from "../pulse/useModalDrawer";
import base from "../sidebar/MoveToProjectModal.module.css";
import styles from "./AdoptToMissionModal.module.css";

/** States the store refuses an adoption into — `_adopt_tx`: "reopen it before adopting". */
const TERMINAL_STATES = new Set(["done", "failed", "abandoned"]);
const PAGE = 50;
const SEARCH_DEBOUNCE_MS = 250;

function stateLabel(m: MissionListRow): string {
  if (m.needs_you) return "needs you";
  return m.state === "dispatching" ? "starting" : m.state;
}

/** Adopt a session into a mission (#948 P5) — opened from the sidebar row's ⋯ menu and from the
 *  session pane's header, the only two places adoption lives now.
 *
 *  **The list is the server's own**, searched and paged by `GET /api/missions` in the active
 *  scope. A mission the store would refuse (done / failed / abandoned) renders DISABLED WITH THE
 *  REASON instead of being filtered out here: filtering a paged list client-side makes a page
 *  silently shorter than it says it is, and hides the one mission the operator was looking for.
 *
 *  **The server decides.** Adopt posts the existing `POST /api/missions/{id}/adopt`; its
 *  reservation and roster fences are the authority. A refusal — a 409 naming the mission that
 *  already holds the session, a 503 while a fence is busy — is shown in place and the dialog stays
 *  open, so the operator can pick again.
 *
 *  Same modal contract as `MoveToProjectModal`: `role="dialog"`, `aria-modal`, focus enters on
 *  mount and returns to the opener on close, Tab is contained, Escape and the backdrop cancel. */
export function AdoptToMissionModal({
  session,
  sessionKey,
  onClose,
  onAdopted,
  returnFocusTo,
  resolveReturnFocus,
}: {
  session: Pick<Session, "title" | "engine" | "project">;
  /** The key the SERVER acts on — the id the URL has settled on, never a frozen transport key. */
  sessionKey: string;
  onClose: () => void;
  onAdopted?: (mission: SessionMissionRef) => void;
  returnFocusTo?: HTMLElement | null;
  /** Where focus goes when the opener is no longer in the document — the header's Adopt chip is
   *  replaced by Open mission, and can fold into "…" (#953 review). */
  resolveReturnFocus?: () => HTMLElement | null;
}) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [rows, setRows] = useState<MissionListRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  // Every read carries a generation, so a slow earlier search cannot replace a newer result.
  const gen = useRef(0);
  // The digest of the ordered ids page 0 was sliced from. Offsets only compose over ONE snapshot.
  const snapshot = useRef<string | null>(null);
  // Bumped to re-read from the top: Retry, or a Load more that found the list had moved.
  const [restart, setRestart] = useState(0);
  const resolveRef = useRef(resolveReturnFocus);
  useLayoutEffect(() => {
    resolveRef.current = resolveReturnFocus;
  }, [resolveReturnFocus]);
  const titleId = "adopt-to-mission-title";

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    const mine = ++gen.current;
    api
      .missions({ q: debounced, limit: PAGE, offset: 0 })
      .then((r) => {
        if (mine !== gen.current) return;
        // An unreadable store answers 200 with `store_error` and no rows. That is not "no open
        // missions", and offering to start one would say it was (#953 review).
        if (r.store_error) {
          setRows(null);
          setLoadError(`Missions could not be read: ${r.store_error}`);
          return;
        }
        snapshot.current = r.snapshot ?? null;
        setRows(r.missions);
        setTotal(r.total);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (mine !== gen.current) return;
        setLoadError(e instanceof Error ? e.message : "Could not load missions.");
      });
  }, [debounced, restart]);

  const loadMore = () => {
    if (!rows) return;
    const mine = gen.current;
    setLoadingMore(true);
    api
      .missions({ q: debounced, limit: PAGE, offset: rows.length })
      .then((r) => {
        if (mine !== gen.current) return;
        // A missing or different digest means the list moved between pages — an archive, a new
        // mission — so appending by offset would skip one row or repeat another, and this dialog
        // has no poll to repair it. Re-read from the top instead (#953 review; the rail does the
        // same, #896 review 19).
        if (r.store_error || r.snapshot == null || r.snapshot !== snapshot.current) {
          setRestart((n) => n + 1);
          return;
        }
        setRows((prev) => {
          const seen = new Set((prev ?? []).map((m) => m.id));
          return [...(prev ?? []), ...r.missions.filter((m) => !seen.has(m.id))];
        });
        setTotal(r.total);
      })
      .catch(() => {})
      .finally(() => setLoadingMore(false));
  };

  // Focus enters on mount, onto the one control present in every state of this dialog.
  useEffect(() => {
    searchRef.current?.focus();
  }, []);
  useFocusContainment({ active: true, panelRef: dialogRef });
  useEffect(
    () => () => {
      const el = returnFocusTo?.isConnected ? returnFocusTo : (resolveRef.current?.() ?? null);
      el?.focus?.();
    },
    [returnFocusTo],
  );
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

  const target = rows?.find((m) => m.id === chosen) ?? null;

  const adopt = async () => {
    if (!target || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.adoptMissionSession(target.id, sessionKey);
      const ref: SessionMissionRef = { id: target.id, title: target.title, state: target.state };
      announceSessionMissionChanged(sessionKey, ref);
      onAdopted?.(ref);
      onClose();
    } catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.status === 503) {
        setError("Mission control is busy with this mission — try again.");
      } else {
        // The server's own detail, verbatim: a 409 names the mission that already holds it.
        setError(e instanceof Error && e.message ? e.message : "That session could not be adopted.");
      }
    }
  };

  return (
    <div className={base.backdrop} onMouseDown={onClose}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${base.dialog} ${styles.dialog}`}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="adopt-dialog"
      >
        <h3 id={titleId} className={`${base.title} ${styles.heading}`}>
          <Crosshair size={15} aria-hidden="true" />
          Adopt to mission
        </h3>
        <p className={base.path}>
          {session.title || "(untitled)"} · {engineBadge(session.engine)}
          {session.project.kind === "project" ? ` · ${session.project.name}` : ""}
        </p>
        <label className={styles.searchWrap}>
          <Search size={13} aria-hidden="true" />
          <input
            ref={searchRef}
            className={styles.search}
            type="search"
            placeholder="Search missions…"
            aria-label="Search missions to adopt into"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        {loadError ? (
          <p className={base.empty} role="status" data-testid="adopt-load-error">
            {loadError}{" "}
            <button
              type="button"
              className={styles.button}
              onClick={() => {
                setLoadError(null);
                setRows(null);
                setRestart((n) => n + 1);
              }}
            >
              Retry
            </button>
          </p>
        ) : rows === null ? (
          <p className={base.empty} role="status">
            Loading missions…
          </p>
        ) : rows.length === 0 ? (
          <p className={base.empty} data-testid="adopt-empty">
            {debounced ? (
              "No open mission matches that search."
            ) : (
              <>
                No open missions yet. <a href={MISSION_PATH}>Start one in Missions</a>.
              </>
            )}
          </p>
        ) : (
          <ul className={`${base.list} ${styles.list}`}>
            {rows.map((m) => {
              const refusal = TERMINAL_STATES.has(m.state)
                ? `${m.state.charAt(0).toUpperCase()}${m.state.slice(1)} — reopen it before adopting a session`
                : null;
              return (
                <li key={m.id}>
                  <button
                    type="button"
                    className={`${base.option} ${styles.option}`}
                    aria-pressed={chosen === m.id}
                    disabled={refusal !== null}
                    onClick={() => setChosen(m.id)}
                    data-testid="adopt-option"
                  >
                    <span className={styles.body}>
                      <span className={base.optName}>{m.title}</span>
                      <span className={styles.meta}>
                        {stateLabel(m)} · {m.session_keys.length}{" "}
                        {m.session_keys.length === 1 ? "session" : "sessions"} ·{" "}
                        {relTime(m.updated_at)}
                      </span>
                      {refusal ? <span className={styles.why}>{refusal}</span> : null}
                    </span>
                  </button>
                </li>
              );
            })}
            {rows.length < total ? (
              <li>
                <button
                  type="button"
                  className={`${base.option} ${styles.option} ${styles.more}`}
                  onClick={loadMore}
                  disabled={loadingMore}
                >
                  {loadingMore ? "Loading…" : `Load more — ${rows.length} of ${total}`}
                </button>
              </li>
            ) : null}
          </ul>
        )}
        {error ? (
          <p className={styles.error} role="alert" data-testid="adopt-error">
            {error}
          </p>
        ) : null}
        <div className={`${base.actions} ${styles.actions}`}>
          <button type="button" className={`${base.cancel} ${styles.button}`} onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.primary}`}
            onClick={() => void adopt()}
            disabled={!target || busy}
            data-testid="adopt-confirm"
          >
            {busy ? "Adopting…" : "Adopt"}
          </button>
        </div>
      </div>
    </div>
  );
}
