/** HQ (Ask until #1525), as a right-hand sidebar on every route (#1294).
 *
 *  The operator wanted Ask one tap away wherever they are, not a page under Dashboard (#1171). The
 *  corner icon beside the bell (`AskToggle`) opens this panel, which slides in from the right edge
 *  and hosts the same `AskConsole` the page did.
 *
 *  **Mounted on first open, then kept.** The console stays mounted while the panel is closed
 *  (hidden and `inert`), so a conversation outlives closing the panel and navigating — ask, open a
 *  match, come back to the thread. Since #1443 the conversation is also kept on the server, so a
 *  reload reopens the latest one (`AskConsole`'s note). NEEDS YOU membership (the answer rows' marker and ⓘ) is read
 *  only while the panel is OPEN, so a closed panel does not poll.
 *
 *  **Two forms, one panel.** Above 800px it is NON-modal: the operator works beside it, and it
 *  closes on ✕, Escape inside it, or the icon. At ≤800px (the shell's mobile line) it covers most
 *  of the page, so it is a modal drawer with a scrim and owes the whole `useModalDrawer` contract — the bell's drawer's, reused.
 *  Portalled to `<body>` either way: `.hud-topbar` is a `backdrop-filter` stacking context the pane
 *  paints over (#752, #987).
 */
import { MessageSquare } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useConfig } from "../../app/config";
import { coerceRecentWindowDays } from "../../lib/recentWindow";
import { useModalDrawer } from "../pulse/useModalDrawer";
import { api } from "../../lib/api";
import { AskConsole } from "./AskConsole";
import { ASK_DRAWER_MQ, useAskPanel } from "./askPanel";
import { ASK_SEEN_EVENT } from "./askUpdates";
import { NeedsYouDetailsDialog } from "./NeedsYouDetailsDialog";
import { needsYouIds } from "./needsYouLabels";
import { useNeedsYou } from "./useNeedsYou";
import s from "./AskSidebar.module.css";
import bell from "../pulse/NotificationBell.module.css";

/** The shell's own mobile line (`useIsMobile`, docs/design.md §5), not the bell's 640px: between
 *  641 and 800px a 440px panel covers most of the page, so there it is a modal drawer too. */

/** How often the icon asks whether a followed session has an unseen update (#1443 Phase 4). */
export const UNREAD_POLL_MS = 60_000;

/** Whether any conversation has an unseen update: read on mount, on a timer while the tab is
 *  visible, when the tab comes back, and at once when a conversation is marked seen. */
function useAskUnread(pollMs: number): boolean {
  const [unread, setUnread] = useState(false);
  useEffect(() => {
    let alive = true;
    // Only the NEWEST read's answer applies: an older "unread" resolving after a newer "seen"
    // never brings the dot back.
    let gen = 0;
    const read = () => {
      if (document.visibilityState !== "visible") return;
      const g = ++gen;
      void api
        .askUnread()
        .then((r) => {
          if (alive && g === gen) setUnread(Boolean(r.unread));
        })
        .catch(() => {
          /* keep what is shown */
        });
    };
    read();
    const timer = window.setInterval(read, pollMs);
    document.addEventListener("visibilitychange", read);
    window.addEventListener(ASK_SEEN_EVENT, read);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", read);
      window.removeEventListener(ASK_SEEN_EVENT, read);
    };
  }, [pollMs]);
  return unread;
}

/** The corner icon, beside the bell. Keeps its place on a phone, as the bell does. A dot marks an
 *  unseen update from a session Ask follows — the label says so too, never the dot alone. */
export function AskToggle({ unreadPollMs = UNREAD_POLL_MS }: { unreadPollMs?: number } = {}) {
  const { open, toggle, triggerRef } = useAskPanel();
  const unread = useAskUnread(unreadPollMs);
  return (
    <div className={bell.wrap} data-topbar-keep="">
      <button
        ref={triggerRef}
        type="button"
        className={`${bell.btn} ${open ? bell.btnOn : ""}`}
        onClick={toggle}
        aria-expanded={open}
        aria-controls="ask-sidebar"
        aria-label={unread ? "HQ — new updates from sessions you follow" : "HQ"}
        title={unread ? "HQ — new updates" : "HQ"}
        data-testid="ask-toggle"
      >
        <MessageSquare size={18} aria-hidden="true" />
        {unread ? <span className={s.dot} aria-hidden="true" data-testid="ask-unread-dot" /> : null}
      </button>
    </div>
  );
}

/** NEEDS YOU membership, read only while mounted — i.e. while the panel is open. */
function NeedsYouFeed({
  onMembership,
  refreshRef,
}: {
  onMembership: (ids: Set<string> | undefined) => void;
  refreshRef: { current: () => void };
}) {
  const cfg = useConfig();
  const windowDays = coerceRecentWindowDays(cfg?.pulse?.window_days);
  const { state, membership, refresh } = useNeedsYou(windowDays, "", "");
  const ids = membership?.ids ?? needsYouIds(state.data?.rows);
  useEffect(() => {
    onMembership(ids);
    // `ids` is a fresh Set per render; the payload objects are what change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [membership, state.data]);
  useEffect(() => {
    refreshRef.current = () => void refresh();
  }, [refresh, refreshRef]);
  return null;
}

export function AskSidebar() {
  const { open, close, triggerRef } = useAskPanel();
  const cfg = useConfig();
  const configured = cfg?.pulse?.configured ?? false;

  // Mounted on the first open and never again unmounted (module note).
  const [mounted, setMounted] = useState(open);
  if (open && !mounted) setMounted(true);

  const [drawer, setDrawer] = useState(
    () => window.matchMedia?.(ASK_DRAWER_MQ).matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.(ASK_DRAWER_MQ);
    if (!mq) return;
    const on = () => setDrawer(mq.matches);
    mq.addEventListener("change", on);
    // A resize between the first render's read and this subscription fires no event we hear.
    on();
    return () => mq.removeEventListener("change", on);
  }, []);

  const panelRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  useModalDrawer({
    active: open && drawer,
    panelRef,
    initialFocusRef: closeRef,
    triggerRef,
    onClose: close,
  });

  // Desktop: the question box takes focus on open; Escape inside the panel closes it and hands
  // focus back to the icon. Not on outside clicks — the operator works beside it.
  useEffect(() => {
    if (!open || drawer) return;
    panelRef.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus();
  }, [open, drawer]);
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (drawer || e.key !== "Escape" || e.defaultPrevented) return;
      // A dialog the console portals to <body> (Delete's confirm) still bubbles its keys through
      // here in React's tree: its Escape is the dialog's own, never "close HQ".
      if (!panelRef.current?.contains(e.target as Node)) return;
      e.preventDefault();
      close();
      triggerRef.current?.focus();
    },
    [drawer, close, triggerRef],
  );

  /** On a phone the drawer covers the page, so FOLLOWING a link from Ask — "Jump in", "Open
   *  mission", or the details dialog's "Open session" — closes it: the operator came to see that
   *  page. Closing is not discarding; the conversation is still there on reopen. On desktop the
   *  panel stays open beside the page. */
  const closeOnFollow = useCallback(
    (e: React.MouseEvent) => {
      if (drawer && (e.target as Element).closest?.("a[href]")) close();
    },
    [drawer, close],
  );

  const [needsYou, setNeedsYou] = useState<Set<string> | undefined>();
  const refreshNeedsYou = useRef<() => void>(() => undefined);
  const [details, setDetails] = useState<string | null>(null);

  if (!mounted) return null;
  return createPortal(
    <>
      {open && drawer ? (
        <button
          type="button"
          className={s.scrim}
          aria-label="Dismiss HQ"
          tabIndex={-1}
          onClick={close}
        />
      ) : null}
      <aside
        id="ask-sidebar"
        ref={panelRef}
        className={`${s.panel} ${open ? s.open : ""}`}
        role={drawer ? "dialog" : "complementary"}
        aria-modal={open && drawer ? true : undefined}
        aria-label="HQ"
        aria-hidden={open ? undefined : true}
        inert={!open}
        onKeyDown={onKeyDown}
        onClick={closeOnFollow}
        data-testid="ask-sidebar"
        data-open={open ? "true" : "false"}
      >
        {open ? (
          <NeedsYouFeed
            onMembership={setNeedsYou}
            refreshRef={refreshNeedsYou}
          />
        ) : null}
        <AskConsole
          configured={configured}
          visible={open}
          needsYou={needsYou}
          onDetails={setDetails}
          onClose={() => {
            close();
            triggerRef.current?.focus();
          }}
          closeRef={closeRef}
        />
      </aside>
      {details ? (
        // The dialog portals itself to <body>, so a DOM test from the aside never sees its Open
        // session link — but React events bubble through the React tree, so this wrapper does
        // (Hermes on #1296). `display: contents` keeps it out of layout.
        <div className={s.contents} onClick={closeOnFollow}>
        <NeedsYouDetailsDialog
          key={details}
          sessionId={details}
          // Close only THIS dialog: a completion that lands later must never close a newer one.
          onClose={() => setDetails((cur) => (cur === details ? null : cur))}
          onChanged={() => refreshNeedsYou.current()}
        />
        </div>
      ) : null}
    </>,
    document.body,
  );
}
