import {
  Activity,
  HelpCircle,
  Menu,
  Network,
  PanelLeftClose,
  Settings as SettingsIcon,
} from "lucide-react";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { NotificationBell } from "../components/pulse/NotificationBell";
import { SessionList } from "../components/sidebar/SessionList";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { Onboarding } from "../routes/Onboarding";
import { Settings } from "../routes/Settings";
import { SessionView } from "../routes/SessionView";
import { ButtonGlitch } from "../components/hud/ButtonGlitch";
import { DataFlowCanvas } from "../components/hud/DataFlowCanvas";
import { MissionTimer } from "../components/hud/MissionTimer";
import { SysClock } from "../components/hud/SysClock";
import { AccentProvider } from "../theme/AccentProvider";
import { ThemeProvider } from "../theme/ThemeProvider";
import { api } from "../lib/api";
import "./App.css";
import { useConfig, useConfigRefresh } from "./config";
import { ConfigProvider } from "./ConfigContext";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";
import { lazyWithReload } from "./lazyWithReload";
import { useAppVersion } from "./useAppVersion";
import { OverviewPrefsProvider } from "./OverviewPrefsContext";
import { SessionsProvider } from "./SessionsContext";
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

// Lazy so @xyflow/react stays out of the main bundle until the overview is opened (#139).
// Wrapped in lazyWithReload so a stale chunk after a deploy self-heals (#160).
const Overview = lazyWithReload(() => import("../routes/Overview"), "overview");
// Pulse — the AI-curated recent-work overview (#441 Phase 5). Lazy like Overview so its
// page code stays out of the main bundle until opened.
const Pulse = lazyWithReload(() => import("../routes/Pulse"), "pulse");

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

  // Sidebar footer + classification-bar counts (HUD telemetry, #211): loaded sessions and how
  // many are live (within the working window). Derived from the shared store the list fills.
  const { sessions } = useSessionsStore();
  const engaged = sessions.length;
  const live = sessions.filter((s) => s.working).length;

  // Session-list order toggle in the sidebar header (#548) — same server-synced pref as the
  // Settings → Appearance radio (#506). `orderPending` is the optimistic flip; it reconciles
  // away once the refreshed config echoes the value back, and a failed save clears it so the
  // control snaps back to the server truth (no drift between the two surfaces).
  const refreshConfig = useConfigRefresh();
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
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <ButtonGlitch />
      <div
        className={cls}
        style={{ "--sidebar-w": `${sidebarW}px` } as React.CSSProperties}
      >
        {/* Canvas lives INSIDE .app so it's within the panels' backdrop scope: .app is a
          backdrop-root (overflow:hidden + stacking context), so a canvas outside it can't be
          blurred by the panels' backdrop-filter. Inside, the frosted panels blur it. (#211) */}
        <DataFlowCanvas />
        <header className="hud-topbar">
          <button
            type="button"
            className="navToggle"
            aria-label={
              surfaceOpen ? "Collapse session list" : "Open session list"
            }
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
            <button
              type="button"
              className="gear"
              aria-label="Help — replay the tour"
              onClick={() => setTourOpen(true)}
            >
              <HelpCircle size={18} />
            </button>
            <Link
              to="/pulse"
              className="gear"
              aria-label="Open Pulse — recent-work overview"
              onClick={closeMobileDrawer}
            >
              <Activity size={18} />
            </Link>
            <NotificationBell />
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
          {/* Header row (#548): the decorative "Sessions / SEC // 01" label gave way to the
            sort-order toggle — same chrome, functional content. The heading stays for the
            <aside> landmark's accessible name, visually hidden. */}
          <header className="sidebar-head">
            <h2 className="hud-h sr-only">Sessions</h2>
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
          </header>
          {/* On small screens the topbar actions collapse into here (behind the hamburger). */}
          <div className="sidebar-actions">
            <button
              type="button"
              className="gear"
              aria-label="Help — replay the tour"
              onClick={() => {
                setTourOpen(true);
                closeMobileDrawer();
              }}
            >
              <HelpCircle size={18} />
            </button>
            <Link
              to="/pulse"
              className="gear"
              aria-label="Open Pulse — recent-work overview"
              onClick={closeMobileDrawer}
            >
              <Activity size={18} />
            </Link>
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
          </div>
          <div className="sidebarBody">
            <SessionList onNavigate={closeMobileDrawer} />
          </div>
          <footer className="sidebar-foot">
            <span className="hud-tag">
              <b className="num">{engaged}</b> ENGAGED ·{" "}
              <b className="num">{live}</b> LIVE
            </span>
          </footer>
        </aside>
        {/* Desktop sidebar resize handle (#507): a focusable separator in the gutter between the
          sidebar and pane panels. Not rendered on mobile (the drawer is fixed-width) or while
          collapsed (no sidebar to size). */}
        {!isMobile && !collapsed && (
          <div
            className="sidebar-resize"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize session list"
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
            <Suspense
              fallback={<div className="tr-overview tr-ov-state">Loading…</div>}
            >
              <Routes>
                <Route path="/" element={<NewSessionLanding />} />
                {/* Canonical Settings form is /settings/:tab (#357); the bare path mounts the
                  same component, which replace-redirects to the first tab (state preserved). */}
                <Route path="/settings" element={<Settings />} />
                <Route path="/settings/:tab" element={<Settings />} />
                <Route path="/overview" element={<Overview />} />
                <Route path="/pulse" element={<Pulse />} />
                <Route path="/s/:engine/:id" element={<SessionView />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Suspense>
          </ChunkErrorBoundary>
        </main>
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
          <span className="hud-tag">
            <span
              className={`hud-led ${live > 0 ? "up" : "idle"}`}
              aria-hidden="true"
            />
            <b className="num">{live}</b> AGENTS LIVE
          </span>
        </footer>
      </div>
      {(showSetup || wizardReplay) && (
        <Onboarding
          mode="wizard"
          onClose={() => {
            setSetupDismissed(true);
            setWizardReplay(false);
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
        />
      )}
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
              </BrowserRouter>
            </SessionsProvider>
          </OverviewPrefsProvider>
        </AccentProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
