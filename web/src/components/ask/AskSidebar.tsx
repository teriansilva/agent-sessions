/** ASK, as a right-hand sidebar on every route (#1294).
 *
 *  The operator wanted Ask one tap away wherever they are, not a page under Dashboard (#1171). The
 *  corner icon beside the bell (`AskToggle`) opens this panel, which slides in from the right edge
 *  and hosts the same `AskConsole` the page did.
 *
 *  **Mounted on first open, then kept.** The console stays mounted while the panel is closed
 *  (hidden and `inert`), so a conversation outlives closing the panel and navigating — ask, open a
 *  match, come back to the thread. It is still memory only: a reload or New conversation ends it,
 *  and nothing is written anywhere. NEEDS YOU membership (the answer rows' marker and ⓘ) is read
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
import { AskConsole } from "./AskConsole";
import { useAskPanel } from "./askPanel";
import { NeedsYouDetailsDialog } from "./NeedsYouDetailsDialog";
import { needsYouIds } from "./needsYouLabels";
import { useNeedsYou } from "./useNeedsYou";
import s from "./AskSidebar.module.css";
import bell from "../pulse/NotificationBell.module.css";

/** The shell's own mobile line (`useIsMobile`, docs/design.md §5), not the bell's 640px: between
 *  641 and 800px a 440px panel covers most of the page, so there it is a modal drawer too. */
const DRAWER_MQ = "(max-width: 800px)";

/** The corner icon, beside the bell. Keeps its place on a phone, as the bell does. */
export function AskToggle() {
  const { open, toggle, triggerRef } = useAskPanel();
  return (
    <div className={bell.wrap} data-topbar-keep="">
      <button
        ref={triggerRef}
        type="button"
        className={`${bell.btn} ${open ? bell.btnOn : ""}`}
        onClick={toggle}
        aria-expanded={open}
        aria-controls="ask-sidebar"
        aria-label="Ask"
        title="Ask"
        data-testid="ask-toggle"
      >
        <MessageSquare size={18} aria-hidden="true" />
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
    () => window.matchMedia?.(DRAWER_MQ).matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.(DRAWER_MQ);
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
          aria-label="Dismiss Ask"
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
        aria-label="Ask"
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
