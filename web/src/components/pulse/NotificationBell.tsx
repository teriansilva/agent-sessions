import { Bell, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useModalDrawer } from "./useModalDrawer";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import { engineBadge, relTime } from "../../lib/format";
import type { PulseNotification } from "../../types/api";
import styles from "./NotificationBell.module.css";
import { MISSION_PATH } from "../../lib/missionLink";
import { useEngineRoster } from "../../app/engineRoster";

const POLL_MS = 60_000;

/** Deep link for a notification: the session it concerns, or mission control when it has none. */
function targetPath(n: PulseNotification): string {
  if (!n.session_id) return MISSION_PATH;
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
  // Re-render when the engine roster lands or changes (#853 P4): this renders agent names,
  // badges or colours, which come from the roster, not from a client-side list.
  useEngineRoster();
  const [items, setItems] = useState<PulseNotification[]>([]);
  const [unread, setUnread] = useState(0);
  /** Recently decided rows, projected back as history with NO controls (#852). Bounded by the
   *  SERVER (newest 10 / 24h) — this renders the window it is given and never re-derives it. */
  const [settled, setSettled] = useState<PulseNotification[]>([]);
  /** Unread escalations whose ledger state could not be established. Deliberately NOT folded
   *  into `unread`: they project `unknown` and offer no control, so counting them as actionable
   *  hands the operator a number they cannot clear by acting (#852 rule 5). Shown as its own,
   *  non-decision signal — the console is degraded, and saying so beats a silent wrong count. */
  const [uncertain, setUncertain] = useState(0);
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
      setSettled(r.settled ?? []);
      setUncertain(r.uncertain ?? 0);
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
          setSettled(r.settled ?? []);
          setUncertain(r.uncertain ?? 0);
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

  const closeDrawer = useCallback(() => setOpen(false), []);

  // The modal contract — focus in, `#root` inert, Tab contained, focus back to the bell on
  // every close path — now lives in one shared hook (#878), because the console's rail drawer
  // owes exactly the same promise and a second copy of a focus trap is how the two drift.
  // `mobile-pulse-layout.spec.ts` is what proves the extraction kept every part of it.
  useModalDrawer({
    active: open && drawer,
    panelRef,
    initialFocusRef: closeRef,
    triggerRef: btnRef,
    onClose: closeDrawer,
    insideRefs: [wrapRef],
  });

  // The ANCHORED dropdown is not a modal and owes none of the above — but it still closes on an
  // outside click or Escape, which the hook only wires while `active`.
  useEffect(() => {
    if (!open || drawer) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
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
      setSettled(r.settled ?? []);
      setUncertain(r.uncertain ?? 0);
    } catch {
      /* leave the list alone rather than pretending it cleared */
    } finally {
      setArming(false);
    }
  }, []);

  /** Clear the settled window — sending exactly the ids that are ON SCREEN.
   *
   *  Not "clear the settled window", which is what the server used to be asked. Recomputing it
   *  at POST time silently hides anything that settled between the render and the click, and
   *  hidden is permanent: it is the flag that keeps a row out of every later projection. The
   *  window is bounded and turns over on its own, so that interleaving is ordinary, not rare.
   *  The server intersects what we send with the window as it stands, so a row that has since
   *  aged out is not touched either. */
  const clearSettled = useCallback(async () => {
    const shown = settled.map((n) => n.id);
    if (!shown.length) return;
    try {
      await api.clearSettledNotifications(shown);
      await load();
    } catch {
      /* leave the history visible rather than pretending it cleared */
    }
  }, [settled, load]);

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

      {/* The console could not establish these rows' state. NOT added to the badge: they offer
          no control, so counting them as actionable is a number the operator cannot clear by
          acting. Said plainly instead, which is the true thing. */}
      {uncertain > 0 && (
        <p className={styles.uncertain} data-testid="bell-uncertain">
          {uncertain} {uncertain === 1 ? "decision" : "decisions"} could not be
          read — the store is unavailable, so they cannot be acted on yet.
        </p>
      )}

      {/* Recent decisions: history, no controls. A decided row is RETIRED rather than deleted
          (#800) because it is also the "already told you" memo that stops one situation being
          announced every TTL (#760) — so this window DRAWS what the store already holds, and
          clearing it hides rather than deletes. */}
      {settled.length > 0 && (
        <div className={styles.settled} data-testid="bell-settled">
          <div className={styles.settledHead}>
            <span>Recent decisions</span>
            <button
              type="button"
              className={styles.settledClear}
              onClick={() => void clearSettled()}
              data-testid="bell-clear-settled"
            >
              Clear
            </button>
          </div>
          <ul className={styles.list}>
            {settled.map((n) => (
              <li
                key={n.id}
                className={styles.row}
                data-testid="bell-settled-row"
              >
                <div className={styles.title}>{n.title}</div>
                <div className={styles.foot}>
                  {n.project && (
                    <span className={styles.proj}>{n.project}</span>
                  )}
                  <span className={styles.age}>{relTime(n.ts)}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
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
