import { X } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { engineName, relTime } from "../../lib/format";
import { listenSessionLimit } from "../../lib/sessionLimit";
import { settingsPath } from "../../routes/settingsTabs";
import type { RunningSessionRow, SessionCapacity } from "../../types/resources";
import dlg from "../HudDialog.module.css";
import { useModalDrawer } from "../pulse/useModalDrawer";
import styles from "./SessionLimitDialog.module.css";

/** The way out of a refused start (#1819): what runs, archive from here, or raise the limit.
 *  A mission holds its sessions, so they have no Archive (the route refuses it). */
export function SessionLimitDialog({
  onClose,
  onRetry,
}: {
  onClose: () => void;
  onRetry: (() => void) | null;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  // What opened it: read on the first render, before `#root` goes inert and takes focus away.
  const openerRef = useRef(document.activeElement as HTMLElement | null);
  useModalDrawer({
    active: true,
    panelRef: dialogRef,
    initialFocusRef: dialogRef,
    triggerRef: openerRef,
    onClose,
  });
  const [data, setData] = useState<SessionCapacity | null>(null);
  const [failed, setFailed] = useState(false);
  const [archiving, setArchiving] = useState<string | null>(null);
  const [archived, setArchived] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [reload, setReload] = useState(0);
  const load = useCallback(() => setReload((n) => n + 1), []);
  useEffect(() => {
    let active = true;
    api
      .sessionCapacity()
      .then((next) => {
        if (!active) return;
        setData(next);
        setFailed(false);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [reload]);
  const archive = async (row: RunningSessionRow) => {
    setArchiving(row.id);
    setError(null);
    try {
      await api.archive(row.id);
      setArchived(row.title);
      load();
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : "That session couldn't be archived.");
    } finally {
      setArchiving(null);
    }
  };

  const unreadable = failed || (data !== null && !data.readable);
  const running = data?.running ?? null;
  const limit = data?.limit ?? null;
  const free = running !== null && limit !== null && running < limit;
  const rows = data?.sessions ?? [];
  const canArchive = rows.some((r) => !r.mission);
  const over = running !== null && limit !== null && running > limit;
  // Recovery never needs an archive: the limit is the operator's to raise.
  const raiseFirst = !free && !unreadable && (!canArchive || (over && data?.source === "recommended"));
  const toFree = running !== null && limit !== null ? running - limit + 1 : 0;

  return createPortal(
    <div className={dlg.backdrop}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={dlg.dialog}
        data-testid="session-limit-dialog"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            {free ? "Slot free" : "Session limit reached"}
          </span>
          <button
            type="button"
            className={dlg.close}
            onClick={onClose}
            aria-label="Close session limit dialog"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className={styles.body}>
          {running !== null && limit !== null && (
            <p className={styles.count} data-testid="session-limit-count">
              {running} <em>of {limit} running</em>
            </p>
          )}
          {unreadable ? (
            <p className={styles.copy} role="alert">
              Couldn't read the running sessions. Nothing was started. Reload, or raise the limit.
            </p>
          ) : free ? (
            <p className={styles.copy} role="status">
              {archived ? `“${archived}” is archived and its history kept. ` : ""}
              {onRetry ? "You can start this now." : "You can start a session now."}
            </p>
          ) : rows.length === 0 && data !== null ? (
            <p className={styles.copy}>
              None of the running sessions can be listed here. A session has no row until its first
              turn, and some may sit outside this list's folders. End one from its own page, or
              raise the limit.
            </p>
          ) : !canArchive && data !== null ? (
            <p className={styles.copy}>
              Every running session here belongs to a mission, so none can be archived from here.
              Finish or stop the mission, or raise the limit.
            </p>
          ) : over && data?.source === "recommended" ? (
            <p className={styles.copy}>
              This limit was set from what this machine is recommended. Nothing was stopped. Raise
              it if this machine can carry more, or archive {toFree} sessions to start another.
            </p>
          ) : (
            <p className={styles.copy}>
              Nothing was started. Archive a session to free a slot; its history is kept and you can
              restore it later. Or raise the limit.
            </p>
          )}
          {error && (
            <p className={styles.copy} role="alert">
              {error}
            </p>
          )}
          {rows.length > 0 && (
            <ul className={styles.list} data-testid="session-limit-list">
              {rows.map((row) => (
                <li key={row.id} className={styles.row}>
                  <div className={styles.what}>
                    <b>{row.title || "Untitled session"}</b>
                    <span>
                      {/* The time first: on a phone the line truncates, and it's what decides. */}
                      {row.working ? "working" : `active ${relTime(row.last_mtime)}`} ·{" "}
                      {engineName(row.engine)} · {row.project}
                    </span>
                  </div>
                  {row.mission ? (
                    <span className={styles.held}>Held by mission</span>
                  ) : (
                    <button
                      type="button"
                      className={styles.secondary}
                      disabled={archiving !== null}
                      onClick={() => void archive(row)}
                    >
                      {archiving === row.id ? "Archiving…" : "Archive"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {data !== null && data.others > 0 && (
            <p className={styles.more} data-testid="session-limit-others">
              {data.others} more {data.others === 1 ? "isn't" : "aren't"} listed here: just started,
              outside this list's folders, or not answering. They count as running.
            </p>
          )}
          <div className={styles.actions}>
            <Link
              className={raiseFirst ? styles.primary : styles.secondary}
              to={settingsPath("resources")}
              onClick={onClose}
            >
              Raise the limit
            </Link>
            <span className={styles.grow} />
            {unreadable ? (
              <button type="button" className={styles.primary} onClick={load}>
                Reload
              </button>
            ) : onRetry ? (
              <button
                type="button"
                className={free ? styles.primary : styles.secondary}
                disabled={!free}
                onClick={() => {
                  onClose();
                  onRetry();
                }}
              >
                Try again
              </button>
            ) : (
              <button type="button" className={free ? styles.primary : styles.secondary} onClick={onClose}>
                Close
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** One dialog for the whole app, opened by `openSessionLimit` from wherever a start was refused. */
export function SessionLimitHost() {
  const [open, setOpen] = useState<{ retry: (() => void) | null } | null>(null);
  useEffect(() => listenSessionLimit((retry) => setOpen({ retry })), []);
  const close = useCallback(() => setOpen(null), []);
  return open ? <SessionLimitDialog onClose={close} onRetry={open.retry} /> : null;
}
