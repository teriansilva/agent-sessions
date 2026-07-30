import { Bell } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { engineBadge, relTime } from "../../lib/format";
import type { PulseNotification } from "../../types/api";
import styles from "./NotificationBell.module.css";

const POLL_MS = 60_000;

/** Deep link for a notification: the session it concerns, or Pulse when it has none. */
function targetPath(n: PulseNotification): string {
  if (!n.session_id) return "/pulse";
  const uuid = n.session_id.slice(n.session_id.indexOf(":") + 1);
  return `/s/${encodeURIComponent(n.engine)}/${encodeURIComponent(uuid)}`;
}

/** The notification bell (#726 Phase 3), beside the Pulse chip in the top bar.
 *
 *  This is the channel that ALWAYS works — no permission prompt, no third-party push service,
 *  no iOS home-screen requirement. Web Push only wakes the operator when the tab is closed; the
 *  bell is what guarantees an escalation is never silently lost, so it polls on its own rather
 *  than depending on a push arriving.
 *
 *  Every row names its project and session and links straight into the terminal — the point of
 *  the whole feature is that the operator can intervene in one tap, not that they read a
 *  summary and go hunting. */
export function NotificationBell() {
  const [items, setItems] = useState<PulseNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api.notifications();
      setItems(r.notifications);
      setUnread(r.unread);
    } catch {
      // A failing notifications endpoint must never break the top bar — the bell simply shows
      // nothing rather than taking the app's chrome down with it.
    }
  }, []);

  useEffect(() => {
    let live = true;
    const tick = () => {
      // Defensive by design: this is top-bar chrome. A synchronous throw here (an older
      // server without the route, a partial test double) would take the entire app shell
      // down over a notification count. Degrade to an empty bell instead.
      Promise.resolve()
        .then(() => api.notifications())
        .then((r) => {
          if (!live) return;
          setItems(r.notifications);
          setUnread(r.unread);
        })
        .catch(() => undefined);
    };
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  // Close on outside click / Escape — a panel that traps the operator is worse than no panel.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node))
        setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next) void load(); // always show what's true now, not what was true a minute ago
  }, [open, load]);

  const markAll = useCallback(async () => {
    try {
      const r = await api.markNotificationsRead();
      setItems(r.notifications);
      setUnread(r.unread);
    } catch {
      /* leave the badge as-is rather than lying about it */
    }
  }, []);

  return (
    <div className={styles.wrap} ref={wrapRef} data-topbar-keep="">
      <button
        type="button"
        className={`${styles.btn} ${open ? styles.btnOn : ""}`}
        onClick={toggle}
        aria-expanded={open}
        aria-label={
          unread ? `Notifications, ${unread} unread` : "Notifications"
        }
      >
        <Bell size={18} aria-hidden="true" />
        {unread > 0 && (
          <span className={styles.badge} aria-hidden="true">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className={styles.panel} role="dialog" aria-label="Notifications">
          <div className={styles.head}>
            <span>Notifications{unread > 0 ? ` · ${unread} unread` : ""}</span>
            {unread > 0 && (
              <button
                type="button"
                className={styles.markAll}
                onClick={() => void markAll()}
              >
                Mark all read
              </button>
            )}
          </div>
          {items.length === 0 ? (
            <p className={styles.empty}>Nothing needs you right now.</p>
          ) : (
            <ul className={styles.list}>
              {items.map((n) => (
                <li
                  key={n.id}
                  className={`${styles.row} ${n.read ? "" : styles.unread}`}
                >
                  <div className={styles.title}>{n.title}</div>
                  {n.reason && <div className={styles.reason}>{n.reason}</div>}
                  <div className={styles.foot}>
                    {n.project && (
                      <span className={styles.proj}>{n.project}</span>
                    )}
                    <span className={styles.eng} aria-hidden="true">
                      {engineBadge(n.engine)}
                    </span>
                    <span className={styles.age}>{relTime(n.ts)}</span>
                    <Link
                      className={styles.open}
                      to={targetPath(n)}
                      onClick={() => setOpen(false)}
                    >
                      Open
                    </Link>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
