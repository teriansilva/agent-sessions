import { LayoutGrid, List as ListIcon, Menu, Network, PanelLeftClose, Settings as SettingsIcon } from "lucide-react";
import { Suspense, useCallback, useEffect, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { SessionList } from "../components/sidebar/SessionList";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { Settings } from "../routes/Settings";
import { SessionView } from "../routes/SessionView";
import { ButtonGlitch } from "../components/hud/ButtonGlitch";
import { DataFlowCanvas } from "../components/hud/DataFlowCanvas";
import { MissionTimer } from "../components/hud/MissionTimer";
import { SysClock } from "../components/hud/SysClock";
import { AccentProvider } from "../theme/AccentProvider";
import { ThemeProvider } from "../theme/ThemeProvider";
import "./App.css";
import { ConfigProvider } from "./ConfigContext";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";
import { lazyWithReload } from "./lazyWithReload";
import { NewVersionBanner } from "./NewVersionBanner";
import { OverviewPrefsProvider } from "./OverviewPrefsContext";
import { SessionsProvider } from "./SessionsContext";
import { useSessionsStore } from "./sessionsStore";

// Lazy so @xyflow/react stays out of the main bundle until the overview is opened (#139).
// Wrapped in lazyWithReload so a stale chunk after a deploy self-heals (#160).
const Overview = lazyWithReload(() => import("../routes/Overview"), "overview");
const SidebarOverview = lazyWithReload(
  () => import("../components/overview/SidebarOverview"),
  "sidebar-overview",
);

const COLLAPSE_KEY = "tr-sidebar-collapsed";
// Sidebar List ⇄ Map choice, device-local. The server-side `sidebar_view` pref is retired
// (#357 Phase 2) — this is layout state like the collapse flag, not a cross-device setting.
const VIEW_KEY = "tr-sidebar-view";

type SidebarView = "list" | "overview";


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
 *  The session lives in the URL (/s/:engine/:id); "/" is the new-session landing. */
function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  // Desktop collapse, persisted. Mobile uses navOpen (off-canvas) and ignores this.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1",
  );
  // Which surface the header toggle drives — so the mobile hamburger never mutates the
  // persisted desktop-collapse flag (and vice versa). Tracks the ≤800px breakpoint.
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia?.("(max-width: 800px)").matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.("(max-width: 800px)");
    if (!mq) return;
    const on = () => setIsMobile(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const location = useLocation();
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

  // Sidebar body: session list or the squeezed Session Overview map (#139). Device-local
  // layout state persisted in localStorage (like the collapse flag) — the server-side
  // `sidebar_view` pref is retired (#357 Phase 2).
  const [sidebarView, setSidebarView] = useState<SidebarView>(() =>
    localStorage.getItem(VIEW_KEY) === "overview" ? "overview" : "list",
  );
  const chooseView = (v: SidebarView) => {
    setSidebarView(v);
    localStorage.setItem(VIEW_KEY, v);
  };

  // The header toggle drives ONLY the current surface: the mobile drawer (≤800px) or the
  // desktop collapse (>800px). This stops the mobile hamburger from mutating/persisting
  // the desktop-collapse flag (Hermes #128).
  const toggle = () => {
    if (isMobile) setNavOpen((o) => !o);
    else setCollapsed((c) => !c);
  };
  // "Open" state of whichever surface the toggle controls (for the icon + aria-expanded).
  const surfaceOpen = isMobile ? navOpen : !collapsed;

  // Sidebar footer + classification-bar counts (HUD telemetry, #211): loaded sessions and how
  // many are live (within the working window). Derived from the shared store the list fills.
  const { sessions } = useSessionsStore();
  const engaged = sessions.length;
  const live = sessions.filter((s) => s.working).length;

  const cls = ["app", navOpen ? "navOpen" : "", collapsed ? "collapsed" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <ButtonGlitch />
      <div className={cls}>
      {/* Canvas lives INSIDE .app so it's within the panels' backdrop scope: .app is a
          backdrop-root (overflow:hidden + stacking context), so a canvas outside it can't be
          blurred by the panels' backdrop-filter. Inside, the frosted panels blur it. (#211) */}
      <DataFlowCanvas />
      <header className="hud-topbar">
        <button
          type="button"
          className="navToggle"
          aria-label={surfaceOpen ? "Collapse session list" : "Open session list"}
          aria-expanded={surfaceOpen}
          onClick={toggle}
        >
          {surfaceOpen ? <PanelLeftClose size={18} /> : <Menu size={18} />}
        </button>
        <span className="hud-brand">
          <span className="mk" aria-hidden="true">
            ◢
          </span>
          BATTLE<b>LAB</b>
        </span>
        <span className="hud-telemetry">
          <SysClock />
          <MissionTimer />
        </span>
        <span className="hud-topbar-actions">
          <Link
            to="/overview"
            className="gear"
            aria-label="Open session overview"
            onClick={closeMobileDrawer}
          >
            <Network size={18} />
          </Link>
          <Link
            to="/settings"
            state={{ returnTo: location.pathname }}
            className="gear"
            aria-label="Settings"
            onClick={closeMobileDrawer}
          >
            <SettingsIcon size={18} />
          </Link>
        </span>
      </header>
      <aside className="sidebar">
        <span className="hud-cnr tl" />
        <span className="hud-cnr tr" />
        <span className="hud-cnr bl" />
        <span className="hud-cnr br" />
        <header className="sidebar-head">
          <h2 className="hud-h">Sessions</h2>
          <span className="hud-tag">SEC // 01</span>
        </header>
        {/* On small screens the topbar actions collapse into here (behind the hamburger). */}
        <div className="sidebar-actions">
          <Link
            to="/overview"
            className="gear"
            aria-label="Open session overview"
            onClick={closeMobileDrawer}
          >
            <Network size={18} />
            <span>Overview</span>
          </Link>
          <Link
            to="/settings"
            state={{ returnTo: location.pathname }}
            className="gear"
            aria-label="Settings"
            onClick={closeMobileDrawer}
          >
            <SettingsIcon size={18} />
            <span>Settings</span>
          </Link>
        </div>
        <div className="viewToggle" role="tablist" aria-label="Sidebar view">
          <button
            type="button"
            role="tab"
            aria-selected={sidebarView === "list"}
            className={sidebarView === "list" ? "on" : ""}
            onClick={() => chooseView("list")}
          >
            <ListIcon size={14} /> List
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={sidebarView === "overview"}
            className={sidebarView === "overview" ? "on" : ""}
            onClick={() => chooseView("overview")}
          >
            <LayoutGrid size={14} /> Map
          </button>
        </div>
        <div className="sidebarBody">
          {sidebarView === "overview" ? (
            <ChunkErrorBoundary>
              <Suspense fallback={<div className="tr-overview tr-ov-state">Loading map…</div>}>
                <SidebarOverview />
              </Suspense>
            </ChunkErrorBoundary>
          ) : (
            <SessionList onNavigate={closeMobileDrawer} />
          )}
        </div>
        <footer className="sidebar-foot">
          <span className="hud-tag">
            <b className="num">{engaged}</b> ENGAGED · <b className="num">{live}</b> LIVE
          </span>
        </footer>
      </aside>
      <button
        type="button"
        className="backdrop"
        aria-label="Close session list"
        tabIndex={-1}
        onClick={() => setNavOpen(false)}
      />
      <main className="terminal-pane">
        <span className="hud-cnr hero tl" />
        <span className="hud-cnr hero tr" />
        <span className="hud-cnr hero bl" />
        <span className="hud-cnr hero br" />
        <ChunkErrorBoundary>
          <Suspense fallback={<div className="tr-overview tr-ov-state">Loading…</div>}>
            <Routes>
              <Route path="/" element={<NewSessionLanding />} />
              {/* Canonical Settings form is /settings/:tab (#357); the bare path mounts the
                  same component, which replace-redirects to the first tab (state preserved). */}
              <Route path="/settings" element={<Settings />} />
              <Route path="/settings/:tab" element={<Settings />} />
              <Route path="/overview" element={<Overview />} />
              <Route path="/s/:engine/:id" element={<SessionView />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </ChunkErrorBoundary>
      </main>
      <footer className="hud-classbar">
        <span className="hud-tag">UNCLASSIFIED // INTERNAL USE // OP: NIGHTJAR</span>
        <span className="hud-tag">
          <span className={`hud-led ${live > 0 ? "up" : "idle"}`} aria-hidden="true" />
          <b className="num">{live}</b> AGENTS LIVE
        </span>
      </footer>
      </div>
    </>
  );
}

export default function App() {
  return (
    <ConfigProvider>
      <ThemeProvider>
        <AccentProvider>
          <OverviewPrefsProvider>
            <SessionsProvider>
              <BrowserRouter>
                <Layout />
                <NewVersionBanner />
              </BrowserRouter>
            </SessionsProvider>
          </OverviewPrefsProvider>
        </AccentProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
