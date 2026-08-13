import { Bell, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
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
  // Inline confirm for `Clear all` (#752) — NOT an undo, and not a modal.
  //
  // Undo was the first choice and does not survive the API: `dismiss` deletes server-side and
  // there is no restore path, so an undo would need either a new endpoint or a deferred delete
  // whose window leaves the client and server disagreeing about what exists. A confirm that
  // arms in place costs one extra tap, needs no new surface, and cannot desynchronise.
  const [arming, setArming] = useState(false);
  // Desktop anchoring. `.hud-topbar` sets `backdrop-filter` and carries NO z-index, so it forms
  // a stacking context the terminal pane paints over — `.panel`'s `z-index: 60` orders it only
  // WITHIN the topbar and can never rise above a sibling that already beats the topbar. The
  // dropdown therefore rendered behind the terminal (#752). Portalling it out and positioning
  // from the bell's own rect removes the stacking dependency instead of trying to out-number it.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(
    null,
  );
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  // ≤640px is where the topbar collapses; below it the panel is a right-hand drawer instead of
  // an anchored dropdown (#750). Tracked live so a rotate/resize switches form without a
  // reopen.
  const [drawer, setDrawer] = useState(
    () => window.matchMedia?.("(max-width: 640px)").matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.("(max-width: 640px)");
    if (!mq) return;
    const on = () => setDrawer(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

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
    // Resolving an action retires its alert server-side, but the badge would keep counting it
    // until the next poll — up to a minute, on the screen where the operator just decided it.
    // Same defensive `tick` as the timer, for the same reason.
    window.addEventListener(ACTION_RESOLVED_EVENT, tick);
    return () => {
      live = false;
      clearInterval(t);
      window.removeEventListener(ACTION_RESOLVED_EVENT, tick);
    };
  }, []);

  // Close on outside click / Escape — a panel that traps the operator is worse than no panel.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      // The drawer is portalled to <body>, so it is NOT inside `wrapRef` — testing the wrap
      // alone would close the panel on every tap of its own rows. Both hosts count as inside;
      // the scrim is what closes on tap.
      if (wrapRef.current?.contains(t) || panelRef.current?.contains(t)) return;
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

  // The drawer declares `aria-modal`, so it owes the full modal contract — declaring it and
  // not honouring it is worse than not declaring it, because it tells assistive tech the
  // background is inert when it is still reachable. Focus moves in on open, the app root is
  // made genuinely `inert` (the drawer + scrim are portalled OUTSIDE #root, so this isolates
  // the background without touching either), and focus returns to the bell on every close
  // path — Escape, scrim, a row's Open link, or a resize that drops drawer mode.
  //
  // Because #root goes inert the bell itself stops being clickable while the drawer is open,
  // which is why the drawer carries its own Close button; that button is also where focus
  // lands, mirroring `SessionRecapModal`.
  useEffect(() => {
    if (!open || !drawer) return;
    const trigger = btnRef.current;
    const root = document.getElementById("root");
    closeRef.current?.focus();
    root?.setAttribute("inert", "");
    return () => {
      // Order matters: the bell lives inside #root and cannot take focus while it is inert.
      root?.removeAttribute("inert");
      // …and the restore waits a frame. On the Escape path a synchronous `focus()` sticks, but
      // when the drawer is dismissed by a TAP the browser is still settling focus from that
      // pointer sequence and finishes after this passive cleanup — landing on <body> and
      // silently undoing the restore. A frame later the event is done and the bell keeps it.
      requestAnimationFrame(() => trigger?.focus());
    };
  }, [open, drawer]);

  // Tab containment. `inert` already stops the background taking focus in browsers that
  // support it; this keeps the cycle correct inside the drawer either way, and is what makes
  // the trap observable in a test rather than inferred from an attribute.
  useEffect(() => {
    if (!open || !drawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const host = panelRef.current;
      if (!host) return;
      const focusable = Array.from(
        host.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      const inside = active instanceof Node && host.contains(active);
      if (e.shiftKey && (!inside || active === first)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (!inside || active === last)) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, drawer]);

  const measure = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect();
    if (r) setAnchor({ top: r.bottom + 4, right: window.innerWidth - r.right });
  }, []);

  // Re-measure while open: the topbar does not scroll, but a resize moves the bell.
  useEffect(() => {
    if (!open || drawer) return;
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [open, drawer, measure]);

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    setArming(false); // never reopen already armed
    if (next) measure();
    if (next) void load(); // always show what's true now, not what was true a minute ago
  }, [open, load, measure]);

  const clearAll = useCallback(async () => {
    try {
      const r = await api.dismissNotifications({ all: true });
      setItems(r.notifications);
      setUnread(r.unread);
    } catch {
      /* leave the list alone rather than pretending it cleared */
    } finally {
      setArming(false);
    }
  }, []);

  const dismissOne = useCallback(async (id: string) => {
    try {
      const r = await api.dismissNotifications({ ids: [id] });
      setItems(r.notifications);
      setUnread(r.unread);
    } catch {
      /* the row stays; a failed delete must never look like a success */
    }
  }, []);

  const markAll = useCallback(async () => {
    try {
      const r = await api.markNotificationsRead();
      setItems(r.notifications);
      setUnread(r.unread);
    } catch {
      /* leave the badge as-is rather than lying about it */
    }
  }, []);

  // Built once and mounted either way — the dropdown and the drawer differ only in where they
  // live and which class they wear, never in what they say.
  const contents = (
    <>
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
        {items.length > 0 &&
          (arming ? (
            <>
              <span className={styles.confirm}>Clear all?</span>
              <button
                type="button"
                className={styles.confirmYes}
                onClick={() => void clearAll()}
              >
                Yes
              </button>
              <button
                type="button"
                className={styles.markAll}
                onClick={() => setArming(false)}
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className={styles.clearAll}
              onClick={() => setArming(true)}
            >
              Clear all
            </button>
          ))}
        {drawer && (
          <button
            ref={closeRef}
            type="button"
            className={styles.closeBtn}
            onClick={() => setOpen(false)}
            aria-label="Close notifications"
          >
            <X size={16} aria-hidden="true" />
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
              <button
                type="button"
                className={styles.rowX}
                onClick={() => void dismissOne(n.id)}
                aria-label={`Dismiss: ${n.title}`}
              >
                <X size={13} aria-hidden="true" />
              </button>
              <div className={styles.title}>{n.title}</div>
              {n.reason && <div className={styles.reason}>{n.reason}</div>}
              <div className={styles.foot}>
                {n.project && <span className={styles.proj}>{n.project}</span>}
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
    </>
  );

  const panel = drawer
    ? createPortal(
        <>
          {/* A real <button> so the dismiss affordance is reachable by keyboard and announced,
            not a bare div that only a pointer can use. */}
          <button
            type="button"
            className={styles.scrim}
            aria-label="Dismiss notifications"
            onClick={() => setOpen(false)}
          />
          <div
            ref={panelRef}
            className={styles.drawer}
            role="dialog"
            aria-modal="true"
            aria-label="Notifications"
          >
            {contents}
          </div>
        </>,
        document.body,
      )
    : createPortal(
        <div
          ref={panelRef}
          className={styles.panel}
          role="dialog"
          aria-label="Notifications"
          style={anchor ? { top: anchor.top, right: anchor.right } : undefined}
        >
          {contents}
        </div>,
        document.body,
      );

  return (
    <div className={styles.wrap} ref={wrapRef} data-topbar-keep="">
      <button
        ref={btnRef}
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

      {open && panel}
    </div>
  );
}
