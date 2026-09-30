import { Menu, PanelLeftClose } from "lucide-react";
import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  createBrowserRouter,
  Navigate,
  Route,
  RouterProvider,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { NotificationBell } from "../components/pulse/NotificationBell";
import { SessionList } from "../components/sidebar/SessionList";
import { MissionRailSlotProvider } from "../components/pulse/railSlot";
import { useModalDrawer } from "../components/pulse/useModalDrawer";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { Onboarding } from "../routes/Onboarding";
import { whatsNewBundleVersion, whatsNewLabel } from "../whatsnew/due";
import { useWhatsNew } from "../whatsnew/useWhatsNew";
import { WhatsNewCtx } from "../whatsnew/WhatsNewContext";
import { WhatsNewDialog } from "../whatsnew/WhatsNewDialog";
import { Settings } from "../routes/Settings";
import { SessionView } from "../routes/SessionView";
import { ButtonGlitch } from "../components/hud/ButtonGlitch";
import { DataFlowCanvas } from "../components/hud/DataFlowCanvas";
import { AccentProvider } from "../theme/AccentProvider";
import { TermFontProvider } from "../theme/TermFontProvider";
import { TermSizeProvider } from "../theme/TermSizeProvider";
import { ThemeProvider } from "../theme/ThemeProvider";
import { useIsMobile } from "../lib/useIsMobile";
import { SectionStateContext } from "./sectionState";
import { api } from "../lib/api";
import "./App.css";
import { useConfig, useConfigRefresh } from "./config";
import { ConfigProvider } from "./ConfigContext";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";
import { lazyWithReload } from "./lazyWithReload";
import { useAppVersion } from "./useAppVersion";
import { OverviewPrefsProvider } from "./OverviewPrefsContext";
import { DashboardRetentionProvider } from "./DashboardRetentionContext";
import { OverviewSessionsProvider } from "./OverviewSessionsContext";
import { SessionsProvider } from "./SessionsContext";
import { WorkspaceProvider } from "./WorkspaceContext";
import { EntryLinks } from "../components/links/EntryLinks";
import { EngineRosterProvider } from "./EngineRosterProvider";
import { useSessionsStore } from "./sessionsStore";
import {
  clampW,
  DEFAULT_W,
  maxSidebarW,
  MIN_W,
  readStoredW,
  WIDTH_KEY,
  WIDTH_STEP,
} from "./sidebarWidth";
import {
  ASK_PATH,
  CHECKLISTS_PATH,
  DASHBOARD_PATH,
  LEGACY_MISSION_PATH,
  MISSION_PATH,
  NEW_PROJECT_PATH,
  TEMPLATES_PATH,
} from "../lib/routes";
import { SETTINGS_PATH } from "../routes/settingsTabs";
import { OperatorMenu } from "../components/shell/OperatorMenu";
import { SectionNav } from "../components/shell/SectionNav";
import {
  activeSection,
  activeSubsection,
} from "../components/shell/sections";

// Lazy so @xyflow/react stays out of the main bundle until the overview is opened (#139).
// Wrapped in lazyWithReload so a stale chunk after a deploy self-heals (#160).
const Overview = lazyWithReload(() => import("../routes/Overview"), "overview");
// Pulse — the AI-curated recent-work overview (#441 Phase 5). Lazy like Overview so its
// page code stays out of the main bundle until opened.
const MissionControl = lazyWithReload(() => import("../routes/Pulse"), "pulse");
// TEMPLATES — the instruction-template gallery + editor (#905). Lazy like the others.
const Templates = lazyWithReload(
  () => import("../routes/Templates"),
  "templates",
);
// MISSIONS → CHECKLISTS — the mission playbooks editor, moved out of Settings. Lazy like the others.
const Checklists = lazyWithReload(
  () => import("../routes/Checklists"),
  "checklists",
);
const TemplateEditor = lazyWithReload(
  () => import("../routes/TemplateEditor"),
  "template-editor",
);
// ASK — `find` / `history` over past work, its own section since #1058. Lazy like the others.
const Dashboard = lazyWithReload(
  () => import("../routes/Dashboard"),
  "dashboard",
);
const Ask = lazyWithReload(() => import("../routes/Ask"), "ask");
// The New project wizard (#1187). Lazy like the others.
const NewProject = lazyWithReload(
  () => import("../routes/NewProject"),
  "new-project",
);

const COLLAPSE_KEY = "tr-sidebar-collapsed";
// Retired key for the old sidebar List ⇄ Map toggle (#139). The sidebar is now list-only and
// `/overview` is the canonical map (#424 Phase 1); we clear any stale value once on mount.
const LEGACY_VIEW_KEY = "tr-sidebar-view";

/** App shell — tactical-HUD framework (#211 redux). Three grid rows: a full-width command
 *  TOPBAR (brand + SYS/MISSION telemetry + overview/settings actions, carrying the single
 *  collapse/drawer toggle), a floating-panel DECK (a session-list sidebar panel + the routed
 *  main/terminal panel — both bracket-framed with margins so they float on the ambient
 *  data-flow canvas), and a full-width CLASSIFICATION footer. The one command-bar toggle drives
 *  whichever surface the viewport exposes:
 *  - Desktop (>800px): it collapses the 320px sidebar column (persisted in localStorage);
 *    collapsed → the pane spans full width (one affordance, #132).
 *  - Mobile (≤800px): the sidebar is an off-canvas drawer the same toggle opens, and the
 *    topbar's overview/settings actions collapse into the drawer. The drawer auto-closes after
 *    navigating.
 *  Settings (#1129) is the one route that renders WITHOUT the sidebar at every width — it is a
 *  utility surface, not a work section (#1058) — and its toggle stands down with the surface.
 *  The session lives in the URL (/s/:engine/:id); "/" is the new-session landing. */
function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  // Footer version surface (#661): the running version + the tap-to-reload update chip.
  const version = useAppVersion();
  // Desktop collapse, persisted. Mobile uses navOpen (off-canvas) and ignores this.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1",
  );
  // Which surface the header toggle drives — so the mobile hamburger never mutates the
  // persisted desktop-collapse flag (and vice versa). Tracks the ≤800px breakpoint, which
  // now has exactly one owner (`useIsMobile`) shared with the map's window workspace (#208).
  const isMobile = useIsMobile();
  const location = useLocation();
  const [sectionMemory] = useState(() => new Map<string, unknown>());
  const routePath = location.pathname + location.search;
  const isSessionPath =
    location.pathname === "/" || location.pathname.startsWith("/s/");
  const [lastSessionPath, setLastSessionPath] = useState(
    isSessionPath ? routePath : "/",
  );
  if (isSessionPath && lastSessionPath !== routePath)
    setLastSessionPath(routePath);
  const config = useConfig();
  // First-run onboarding (#463): show the setup wizard once the password gate has cleared and
  // the install isn't already onboarded. `setupDismissed` hides it immediately on finish/skip
  // (no config refetch needed); the topbar Help entry re-opens the slideshow tour (`tourOpen`).
  const [tourOpen, setTourOpen] = useState(false);
  // Re-run the full setup wizard on demand (#675) — from the Help → tour overlay.
  const [wizardReplay, setWizardReplay] = useState(false);
  const [setupDismissed, setSetupDismissed] = useState(false);
  const showSetup =
    config?.onboarded === false &&
    !config?.must_change_password &&
    !setupDismissed;
  // Close the mobile drawer whenever the route changes (e.g. a row was tapped).
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNavOpen(false);
  }, [location.pathname]);

  // Same-route nav targets — New session (Link to="/"), the already-active session row, and
  // Overview/Settings when you're already there — don't change location.pathname, so the
  // route-change effect above never fires and the off-canvas drawer would stay open (#283).
  // The sidebar links/rows call this directly on tap. It touches ONLY navOpen, never the
  // persisted desktop `collapsed` flag, so it's a harmless no-op on desktop.
  const closeMobileDrawer = useCallback(() => setNavOpen(false), []);

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  // Desktop sidebar width (#507), persisted device-local. Seeded from storage (clamped).
  const [sidebarW, setSidebarW] = useState(readStoredW);
  useEffect(() => {
    localStorage.setItem(WIDTH_KEY, String(sidebarW));
  }, [sidebarW]);
  // Re-clamp against the viewport on resize so a width saved on a wide monitor can't crowd the
  // pane after moving to a narrow window.
  useEffect(() => {
    const onResize = () => setSidebarW((w) => clampW(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Drag-to-resize: pointer-capture so the gesture owns the cursor and a fast drag can't slip
  // off the thin handle. `resizing` flips a class that suppresses text selection + stray
  // pointer events on the panels while dragging.
  const [resizing, setResizing] = useState(false);
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);
  const onResizeDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startW: sidebarW };
    e.currentTarget.setPointerCapture(e.pointerId);
    setResizing(true);
  };
  const onResizeMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    setSidebarW(clampW(d.startW + (e.clientX - d.startX)));
  };
  const onResizeUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    setResizing(false);
  };
  // Keyboard a11y for the separator: arrows nudge, Home/End jump to the clamp ends.
  const onResizeKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      setSidebarW((w) => clampW(w - WIDTH_STEP));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setSidebarW((w) => clampW(w + WIDTH_STEP));
    } else if (e.key === "Home") {
      e.preventDefault();
      setSidebarW(MIN_W);
    } else if (e.key === "End") {
      e.preventDefault();
      setSidebarW(maxSidebarW());
    }
  };

  // One-time cleanup of the retired sidebar List ⇄ Map toggle pref (#424 Phase 1). The sidebar
  // is list-only now; `/overview` is the canonical map.
  useEffect(() => {
    localStorage.removeItem(LEGACY_VIEW_KEY);
  }, []);

  // The header toggle drives ONLY the current surface: the mobile drawer (≤800px) or the
  // desktop collapse (>800px). This stops the mobile hamburger from mutating/persisting
  // the desktop-collapse flag (Hermes #128).
  const toggle = () => {
    if (isMobile) setNavOpen((o) => !o);
    else setCollapsed((c) => !c);
  };
  // "Open" state of whichever surface the toggle controls (for the icon + aria-expanded).
  const surfaceOpen = isMobile ? navOpen : !collapsed;

  /** On the mission route the sidebar lists MISSIONS, not sessions (#935).
   *
   *  The operator had two vertical lists side by side — this one and the console's own rail —
   *  spending 620px of a desktop on lists about different things. The console still owns the
   *  mission list and portals its existing rail into the slot below; the shell only supplies the
   *  space and the surrounding chrome.
   *
   *  **At every width, since #940.** It used to be desktop-only: the shell's off-canvas drawer had
   *  a backdrop and nothing else, while the console's own `MissionDrawer` was a real modal, so
   *  offering the slot on a phone would have traded a layout fix for a lost focus trap. The drawer
   *  now carries the contract itself (see `useModalDrawer` below), which is what makes the phone
   *  safe to include — and the operator's report was precisely about the phone: two hamburgers on
   *  one screen, one opening sessions and one opening missions. */
  // `/pulse` is the pre-#948 path, still live as a redirect; counting it here keeps the sidebar
  // from flashing the session list for the one render before the redirect lands.
  // …and Missions → Checklists (#1221): a Missions page carries the mission sidebar, not the
  // session list. The checklists page mounts the console's rail on its own (`railOnly`).
  const missionRoute =
    location.pathname === MISSION_PATH ||
    location.pathname === LEGACY_MISSION_PATH ||
    location.pathname === CHECKLISTS_PATH;
  const railInSidebar = missionRoute;
  /** SETTINGS RENDERS WITHOUT THE SESSION SIDEBAR (#1129), at every width. Settings is a
   *  utility surface, not a work section (#1058) — the shell carries no session list there in
   *  either of its two viewport forms: not the docked column, not the off-canvas drawer (the
   *  toggle stands down with the surface, so a dead control can't silently mutate the
   *  persisted desktop collapse pref from a route with no sidebar).
   *
   *  The aside STAYS MOUNTED — `.app.noSidebar` only hides it, the identical DOM state a
   *  desktop-collapsed sidebar already produces — because `SessionList` renders outside
   *  `<Routes>` on purpose (#1007's continuity contract): the list's rows, cursor and 15 s
   *  poll survive a Settings visit, and returning to a session restores them with no remount
   *  fetch. Pinned red-vs-green by `e2e/settings-no-sidebar.spec.ts`. */
  const settingsRoute =
    location.pathname === SETTINGS_PATH ||
    location.pathname.startsWith(`${SETTINGS_PATH}/`);
  /** The New project wizard renders without the session sidebar too (#1187, operator): it is a
   *  focused flow reached from New session, the dashboard and Settings → Projects, and a session
   *  list beside it is noise. So do Dashboard (with Ask's page under it) and Templates (#1233,
   *  operator): neither acts on the list, and the column only took width from the page. Same
   *  mechanism as Settings — hidden, never unmounted, so returning to Sessions is instant. */
  const noSidebar =
    settingsRoute ||
    location.pathname === NEW_PROJECT_PATH ||
    location.pathname === DASHBOARD_PATH ||
    location.pathname === ASK_PATH ||
    location.pathname === TEMPLATES_PATH ||
    location.pathname.startsWith(`${TEMPLATES_PATH}/`);
  /** Which of the five work sections the current route belongs to, `null` on Settings (#1058).
   *  One derivation, read by the top bar's nav and the drawer's copy of it. */
  const section = activeSection(location.pathname);
  /** And which sub-menu entry, where the section has one — the map under Sessions (#1069). */
  const subsection = activeSubsection(location.pathname);
  /** Published to the console through context. A ref callback, not an effect: it fires on
   *  commit with the element (and with `null` on unmount), which is exactly the lifetime the
   *  portal needs and avoids setting state from inside an effect. */
  const [railSlotEl, setRailSlotEl] = useState<HTMLElement | null>(null);
  /** The head-row slot beside it (#948 P2): where the rail's counts render. */
  const [railHeadEl, setRailHeadEl] = useState<HTMLElement | null>(null);
  /** And the footer slot (#948 §1): where the rail's mission and held-session telemetry render. */
  const [railFootEl, setRailFootEl] = useState<HTMLElement | null>(null);

  /** THE MOBILE DRAWER IS A MODAL, and it owes the whole contract (#940).
   *
   *  It had a backdrop and nothing else — no `role="dialog"`, no `aria-modal`, no focus moved in
   *  or restored, no Tab containment, no Esc. That was tolerable while it was only a session list;
   *  it stopped being tolerable when the mission rail moved into it, because the surface a
   *  keyboard or screen-reader operator navigates missions with cannot be one they can Tab
   *  straight out of into a page that is visually covered.
   *
   *  `useModalDrawer` is the same hook `NotificationBell` and `MissionDrawer` use — reused, not
   *  re-implemented, because a focus trap is exactly the thing that rots when copied.
   *
   *  **It cannot be attached unchanged.** Its default isolates the background by making `#root`
   *  inert, which is right for a panel portalled out of the root — and the shell's `<aside>` is
   *  not: it is a grid track of `.app`, inside that root. So this names the regions to isolate
   *  instead. The header and the routed pane are the whole interactive background; the sidebar and
   *  its backdrop deliberately are not among them.
   *
   *  Desktop gets NONE of this. There the sidebar is a docked column, not a dialog — `aria-modal`
   *  on it would tell a screen reader the rest of the page does not exist. */
  const asideRef = useRef<HTMLElement | null>(null);
  const headerRef = useRef<HTMLElement | null>(null);
  const mainRef = useRef<HTMLElement | null>(null);
  const backdropRef = useRef<HTMLButtonElement | null>(null);
  const navToggleRef = useRef<HTMLButtonElement | null>(null);
  const drawerIsModal = isMobile && navOpen;
  /** A PARKED DRAWER IS NOT A REACHABLE ONE (#940 review 2).
   *
   *  The shell's `<aside>` is always mounted and merely translated out of the viewport when
   *  closed — unlike the console's old `MissionDrawer`, which was unmounted. Translation hides it
   *  from the eye and from nothing else: at 412px, tabbing from the header toggle put focus on a
   *  mission row whose right edge sat at −2px, off-screen, with no dialog open. The operator gets
   *  no visible focus and can drive mission selection without ever revealing the control.
   *
   *  `inert` is the whole fix, and it is the right one rather than `tabindex="-1"` sweeps or
   *  `aria-hidden`: it removes the subtree from the tab order AND from the accessibility tree AND
   *  from pointer events, in one attribute the browser owns. Desktop is untouched — a docked
   *  column is reachable by definition, which is why this is gated on `isMobile` and not on
   *  `surfaceOpen`. */
  const drawerIsParked = isMobile && !navOpen;
  const inertRegions = useMemo(() => [headerRef, mainRef], []);
  const insideRegions = useMemo(() => [backdropRef], []);
  useModalDrawer({
    active: drawerIsModal,
    panelRef: asideRef,
    // The panel itself, not a control inside it: the drawer shows no close button, so focus lands
    // on the dialog and a screen reader announces its name. Escape, the scrim and the visually
    // hidden in-dialog Close close it.
    initialFocusRef: asideRef,
    triggerRef: navToggleRef,
    onClose: closeMobileDrawer,
    insideRefs: insideRegions,
    inertRefs: inertRegions,
  });

  /** Handed to the console with the slot. Closing is only meaningful while the sidebar IS the
   *  drawer — on a docked column there is nothing to dismiss, and closing `navOpen` there would
   *  be a no-op anyway, but saying so here keeps the console from having to know which it is. */
  const dismissRail = useCallback(() => {
    if (isMobile) setNavOpen(false);
  }, [isMobile]);
  const railSlot = useMemo(
    () => ({ el: railSlotEl, headEl: railHeadEl, footEl: railFootEl, dismiss: dismissRail }),
    [railSlotEl, dismissRail, railHeadEl, railFootEl],
  );

  // Sidebar footer counts (#211, redefined in #1085): LIVE is an agent process running
  // (thinking, waiting on you, or printing — watched or not), over the sidebar's whole FILTERED
  // list. The bottom bar no longer carries a host-wide count: it disagreed with the dashboard's
  // scoped one, which counts only sessions the list can show.
  const { counts } = useSessionsStore();

  // Session-list order toggle in the sidebar header (#548) — same server-synced pref as the
  // Settings → Appearance radio (#506). `orderPending` is the optimistic flip; it reconciles
  // away once the refreshed config echoes the value back, and a failed save clears it so the
  // control snaps back to the server truth (no drift between the two surfaces).
  const refreshConfig = useConfigRefresh();

  // What's new (#971): a once-per-operator slideshow for the newest release in releases.ts.
  // `useWhatsNew` decides when it opens and keeps an open one open until the operator closes it.
  const navigate = useNavigate();
  const persistWhatsNew = useCallback(
    (shown: string) => {
      // Durable only when the server says so: a failed write leaves just this tab's flag.
      api.dismissWhatsNew(shown).then(refreshConfig, () => {
        /* not acknowledged — the next load decides again */
      });
    },
    [refreshConfig],
  );
  const whatsNew = useWhatsNew({
    config,
    wizardOpen: showSetup || wizardReplay || tourOpen,
    bundle: whatsNewBundleVersion(version.current),
    server: version.server,
    updateReady: version.updateReady,
    persist: persistWhatsNew,
  });
  // The Help menu's What's new item (#987): absent when no release notes are bundled.
  const helpWhatsNew = whatsNewLabel();
  const cfgOrder = config?.session_list_order ?? "recent_activity";
  const [orderPending, setOrderPending] = useState<string | null>(null);
  if (orderPending && orderPending === cfgOrder) setOrderPending(null);
  const listOrder = orderPending ?? cfgOrder;
  const chooseOrder = (mode: string) => {
    if (mode === listOrder) return;
    setOrderPending(mode);
    api
      .setPrefs({ session_list_order: mode })
      // The config refetch is what re-sorts the list: useSessionsList watches the pref (#548).
      .then(() => refreshConfig())
      .catch(() => setOrderPending(null));
  };

  const cls = [
    "app",
    navOpen ? "navOpen" : "",
    collapsed ? "collapsed" : "",
    resizing ? "resizing" : "",
    noSidebar ? "noSidebar" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <SectionStateContext.Provider value={sectionMemory}>
      <WhatsNewCtx.Provider value={whatsNew.open}>
      <ButtonGlitch />
      <div
        className={cls}
        style={{ "--sidebar-w": `${sidebarW}px` } as React.CSSProperties}
      >
        {/* Canvas lives INSIDE .app so it's within the panels' backdrop scope: .app is a
          backdrop-root (overflow:hidden + stacking context), so a canvas outside it can't be
          blurred by the panels' backdrop-filter. Inside, the frosted panels blur it. (#211) */}
        <DataFlowCanvas />
        <header className="hud-topbar" ref={headerRef}>
          {/* No toggle on Settings (#1129): the surface it drives does not exist there in
              either viewport form, and a live hamburger on a settings page would only mutate
              the persisted collapse pref for nothing. */}
          {!noSidebar && (
            <button
              type="button"
              className="navToggle"
              ref={navToggleRef}
              aria-label={
                railInSidebar
                  ? surfaceOpen
                    ? "Collapse mission list"
                    : "Open mission list"
                  : surfaceOpen
                    ? "Collapse session list"
                    : "Open session list"
              }
              aria-expanded={surfaceOpen}
              onClick={toggle}
            >
              {surfaceOpen ? <PanelLeftClose size={18} /> : <Menu size={18} />}
            </button>
          )}
          <span className="hud-brand">
            <span className="mk" aria-hidden="true">
              ◢
            </span>
            {/* The wordmark is its own element so a phone can drop it and keep the ◢ mark: five
                sections, the bell and the operator tile need the width (#1058). */}
            <span className="wm">
              BATTLE<b>LAB</b>
            </span>
          </span>
          <SectionNav
            active={section}
            activeSub={subsection}
            sessionsPath={lastSessionPath}
            onNavigate={closeMobileDrawer}
          />
          {/* The SYS clock and the MISSION uptime readout are gone (#1085): decoration the operator
              asked to drop, and the width goes back to the nav. */}
          <span className="hud-topbar-actions">
            {/* The `?` and ⚙ that sat here moved into the operator menu (#1085). */}
            <NotificationBell />
            {/* LAST, hard against the edge, and it KEEPS ITS PLACE on a phone (#1058): the bell
                and the tile both carry `data-topbar-keep`, so the corner still answers "which
                account, and is anything waiting?" once the rest of the cluster rides the drawer.
                The attribute goes on a wrapper rather than inside `AnchoredMenu`, because which
                shell surface a menu lives in is the shell's business, not the menu's. */}
            <span className="operator-slot" data-topbar-keep="">
              <OperatorMenu
                username={config?.username}
                authMode={config?.auth_mode}
                onNavigate={closeMobileDrawer}
                onTour={() => setTourOpen(true)}
                onWhatsNew={whatsNew.open}
                whatsNewLabel={helpWhatsNew}
              />
            </span>
          </span>
        </header>
        {/* `role="dialog"` + `aria-modal` ONLY while this is the off-canvas drawer (#940). A
            breakpoint is not a modal, and a docked column that claims to be one hides the rest of
            the page from assistive tech. */}
        <aside
          className="sidebar"
          ref={asideRef}
          // See `drawerIsParked`. React renders `inert` as a boolean attribute, so `false` omits
          // it — `undefined` and `false` behave the same here, and the explicit `false` says the
          // desktop case was considered rather than forgotten.
          inert={drawerIsParked}
          role={drawerIsModal ? "dialog" : undefined}
          tabIndex={drawerIsModal ? -1 : undefined}
          aria-modal={drawerIsModal ? true : undefined}
          aria-label={
            drawerIsModal
              ? railInSidebar
                ? "Missions"
                : "Sessions"
              : undefined
          }
        >
          <span className="hud-cnr tl" />
          <span className="hud-cnr tr" />
          <span className="hud-cnr bl" />
          <span className="hud-cnr br" />
          {/* NO VISIBLE ✕, BUT A WAY OUT FROM INSIDE (#962). The operator asked for the ✕ gone, and
              touch users close the drawer on the scrim. That is not enough for a screen-reader user
              on a phone: the opener sits in the inert header, and the scrim is outside the dialog
              with tabIndex -1, so navigation restricted to the modal would have no dismiss at all.
              So the close control stays, visually hidden — announced as "Close, button" inside the
              labelled dialog, revealed only when a keyboard focuses it (see App.css). */}
          {drawerIsModal ? (
            <button
              type="button"
              className="sr-only sidebar-drawer-dismiss"
              onClick={closeMobileDrawer}
              data-testid="drawer-dismiss"
            >
              Close
            </button>
          ) : null}
          {/* Header row (#548): the decorative "Sessions / SEC // 01" label gave way to the
            sort-order toggle — same chrome, functional content. The heading stays for the
            <aside> landmark's accessible name, visually hidden. */}
          <header
            className={`sidebar-head${railInSidebar ? " isMissionSection" : ""}`}
          >
            <h2 className="hud-h sr-only">
              {railInSidebar ? "Missions" : "Sessions"}
            </h2>
            {/* THE WHOLE CONTROL STANDS DOWN, NOT JUST ITS LABEL (#935, #937 review 1,
                finding 3). Hiding only the "Order" tag left Recent / Created rendered above the
                mission rail — and they were not merely inert: clicking Created wrote
                `session_list_order` to /api/prefs, so navigating to missions could silently
                re-sort the operator's SESSION list. An active control for the wrong collection
                is worse than a stale label. Missions have their own ordering and their own scope
                switch inside the rail; when they want an order control it will be theirs. */}
            {railInSidebar ? (
              /* THE HEAD ROW IS A ROW AGAIN (#948 P2). It collapsed to 0px when the ORDER control
                 stood down (#937), which left the mission rail without the 38px head the sessions
                 sidebar has. The visible tag is presentational — the sr-only heading above already
                 names the landmark — and the rail portals its counts into the slot beside it. */
              <>
                <span className="hud-tag" aria-hidden="true">
                  Missions
                </span>
                <span
                  className="missionHeadSlot"
                  ref={setRailHeadEl}
                  data-testid="mission-head-slot"
                />
              </>
            ) : (
              <>
                <span className="hud-tag" id="list-order-label">
                  Order
                </span>
                <span
                  className="hud-seg"
                  role="radiogroup"
                  aria-labelledby="list-order-label"
                >
                  <button
                    type="button"
                    role="radio"
                    aria-checked={listOrder === "recent_activity"}
                    onClick={() => chooseOrder("recent_activity")}
                    title="Newest update first"
                  >
                    Recent
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={listOrder === "created_at"}
                    onClick={() => chooseOrder("created_at")}
                    title="Newest-created first — order stays put as sessions update"
                  >
                    Created
                  </button>
                </span>
              </>
            )}
          </header>
          {/* No action row here any more (#1085): Help and Settings live in the operator menu,
              which stays in the top bar at every width, and the drawer does not repeat the
              section nav either (#1069). */}
          <div className="sidebarBody">
            {railInSidebar && (
              /* The portal DESTINATION, part of the shell's own markup rather than created on
                 demand. The ref callback publishes the element on commit, which is what lets the
                 console read it from context instead of hunting for it by id in an effect. */
              <div
                id="mission-rail-slot"
                className="missionRailSlot"
                ref={setRailSlotEl}
              />
            )}
            {/* THE SESSION LIST STAYS MOUNTED on mission routes (#1233) — hidden, never unmounted,
                the same contract Settings keeps (#1129). Swapping it out for the rail threw away
                its rows and pages, so every return from Missions was a blocking reload. */}
            <SessionList onNavigate={closeMobileDrawer} hidden={railInSidebar} />
          </div>
          <footer className="sidebar-foot">
            {railInSidebar ? (
              /* The footer describes the list above it (#948 §1). Where that list is missions, the
                 rail portals its own telemetry here — mission count and held sessions — instead of
                 the session counts #937 had to relabel. */
              <span ref={setRailFootEl} data-testid="mission-foot-slot" />
            ) : (
              <span
                className="hud-tag"
                data-testid="sidebar-counts"
                title="Sessions in this list, and how many have an agent running"
              >
                <b className="num">{counts?.total ?? "—"}</b> SESSIONS ·{" "}
                <b className="num">{counts?.live ?? "—"}</b> LIVE
              </span>
            )}
          </footer>
        </aside>
        {/* Desktop sidebar resize handle (#507): a focusable separator in the gutter between the
          sidebar and pane panels. Not rendered on mobile (the drawer is fixed-width), while
          collapsed (no sidebar to size), or on Settings (#1129 — no sidebar there either). */}
        {!isMobile && !collapsed && !noSidebar && (
          <div
            className="sidebar-resize"
            role="separator"
            aria-orientation="vertical"
            aria-label={
              railInSidebar ? "Resize mission list" : "Resize session list"
            }
            aria-valuenow={sidebarW}
            aria-valuemin={MIN_W}
            aria-valuemax={maxSidebarW()}
            tabIndex={0}
            onPointerDown={onResizeDown}
            onPointerMove={onResizeMove}
            onPointerUp={onResizeUp}
            onDoubleClick={() => setSidebarW(DEFAULT_W)}
            onKeyDown={onResizeKey}
          >
            <span className="sidebar-resize-grip" aria-hidden="true" />
          </div>
        )}
        <button
          type="button"
          className="backdrop"
          ref={backdropRef}
          aria-label={
            railInSidebar ? "Close mission list" : "Close session list"
          }
          tabIndex={-1}
          onClick={() => setNavOpen(false)}
        />
        <main className="terminal-pane" ref={mainRef}>
          <span className="hud-cnr hero tl" />
          <span className="hud-cnr hero tr" />
          <span className="hud-cnr hero bl" />
          <span className="hud-cnr hero br" />
          <ChunkErrorBoundary>
            <Suspense
              fallback={<div className="tr-overview tr-ov-state">Loading…</div>}
            >
              {/* The console reads the slot from here (#935) — see `railSlot.tsx`. */}
              <MissionRailSlotProvider value={railSlot}>
                <Routes>
                  <Route path="/" element={<NewSessionLanding />} />
                  {/* Canonical Settings form is /settings/:tab (#357); the bare path mounts the
                  same component, which replace-redirects to the first tab (state preserved). */}
                  <Route path="/settings" element={<Settings />} />
                  {/* The checklists were a Settings tab until they moved under Missions. */}
                  <Route
                    path="/settings/ai-playbooks"
                    element={<Navigate to={CHECKLISTS_PATH} replace />}
                  />
                  <Route path="/settings/:tab" element={<Settings />} />
                  {/* #1128: an agent's own page, and the agent Defaults, under the roster. */}
                  <Route path="/settings/agents/:agent" element={<Settings />} />
                  <Route path="/overview" element={<Overview />} />
                  <Route path={MISSION_PATH} element={<MissionControl />} />
                  <Route path={LEGACY_MISSION_PATH} element={<LegacyMissionRedirect />} />
                  <Route path={CHECKLISTS_PATH} element={<Checklists />} />
                  <Route path={DASHBOARD_PATH} element={<Dashboard />} />
                  {/* #1171: a conversation has its own page under Dashboard. */}
                  <Route path={ASK_PATH} element={<Ask />} />
                  <Route path={NEW_PROJECT_PATH} element={<NewProject />} />
                  <Route path="/templates" element={<Templates />} />
                  <Route path="/templates/new" element={<TemplateEditor />} />
                  <Route path="/templates/:id" element={<TemplateEditor />} />
                  <Route path="/s/:engine/:id" element={<SessionView />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>
              </MissionRailSlotProvider>
            </Suspense>
          </ChunkErrorBoundary>
        </main>
        {/* A session or mission link from outside the app: open it the way this device was told
            to, or ask (#1232). Renders nothing unless it is asking. */}
        <EntryLinks />
        <footer className="hud-classbar">
          <span className="hud-footer-left">
            <span className="hud-tag">
              {config?.hostname
                ? `HOST // ${config.hostname.toUpperCase()}`
                : ""}
            </span>
            {version.displayVersion !== null && (
              <span className="hud-tag hud-version">
                V{version.displayVersion}
              </span>
            )}
          </span>
          {version.updateReady && (
            <button
              type="button"
              className="hud-update-chip"
              onClick={version.applyUpdate}
            >
              ⟳ {version.server !== null ? `V${version.server} ` : ""}READY —
              TAP TO RELOAD
            </button>
          )}
        </footer>
      </div>
      {(showSetup || wizardReplay) && (
        <Onboarding
          mode="wizard"
          onClose={() => {
            setSetupDismissed(true);
            setWizardReplay(false);
            // Completing (or skipping) setup covers the current notes, saved or not (#971).
            whatsNew.markSetupDone();
          }}
        />
      )}
      {tourOpen && (
        <Onboarding
          mode="tour"
          onClose={() => setTourOpen(false)}
          onRerunSetup={() => {
            setTourOpen(false);
            setWizardReplay(true);
          }}
          onWhatsNew={() => {
            setTourOpen(false);
            whatsNew.open();
          }}
        />
      )}
      {whatsNew.release && (
        <WhatsNewDialog
          key={whatsNew.release.version}
          release={whatsNew.release}
          onDismiss={whatsNew.dismiss}
          onNavigate={(to) => {
            whatsNew.dismiss();
            navigate(to);
          }}
        />
      )}
      </WhatsNewCtx.Provider>
    </SectionStateContext.Provider>
  );
}

/** `/pulse` was the mission route until #948. Bookmarks, notifications already sitting in the OS
 *  tray and push payloads the service worker cached still carry it, so it stays as a permanent
 *  replace-redirect. It keeps the query and hash, which is how `?m=<mission id>` survives the hop. */
function LegacyMissionRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: MISSION_PATH, search, hash }} replace />;
}

export default function App() {
  // A DATA router, not <BrowserRouter> (#905 P2 review): `useBlocker` — the only way to hold
  // Back/Forward and every in-app link on a dirty editor — needs one. The whole shell hangs off
  // a single catch-all route; the `<Routes>` inside `Layout` keep working as descendant routes.
  const [router] = useState(() =>
    createBrowserRouter([{ path: "*", element: <Layout /> }]),
  );
  return (
    <ConfigProvider>
      {/* The engine roster (#853 P4): every agent-facing surface reads it; loaded once, above
          the router, so a navigation never refetches it. */}
      <EngineRosterProvider>
      <ThemeProvider>
        <AccentProvider>
          <TermSizeProvider>
            <TermFontProvider>
              <OverviewPrefsProvider>
                {/* ABOVE the router for the same reason (#1007), and with the same split: the
                  map's last SUCCESSFUL COMPLETE result is retained here, so re-entering
                  /overview renders warm instead of re-running the whole multi-page sequence
                  behind a blocking spinner. Data only — loading and cancellation stay
                  route-owned, because the route unmount is what cancels. */}
                <OverviewSessionsProvider>
                  {/* The dashboard's last reads, retained the same way and for the same reason
                    (#1223): coming back paints them at once and revalidates behind a bar. */}
                  <DashboardRetentionProvider>
                  <SessionsProvider>
                    {/* ABOVE the router (#936): the map's window records have to survive the
                      navigations they exist to be resilient to, and a provider inside the routed
                      tree would be remounted by exactly those. */}
                    <WorkspaceProvider>
                      <RouterProvider router={router} />
                    </WorkspaceProvider>
                  </SessionsProvider>
                  </DashboardRetentionProvider>
                </OverviewSessionsProvider>
              </OverviewPrefsProvider>
            </TermFontProvider>
          </TermSizeProvider>
        </AccentProvider>
      </ThemeProvider>
      </EngineRosterProvider>
    </ConfigProvider>
  );
}
