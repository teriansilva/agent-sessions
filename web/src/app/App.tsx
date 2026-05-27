import { LayoutGrid, List as ListIcon, Menu, Network, PanelLeftClose, Settings as SettingsIcon, X } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { SessionList } from "../components/sidebar/SessionList";
import { engineBadge, engineName } from "../lib/format";
import { api } from "../lib/api";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { Settings } from "../routes/Settings";
import { SessionView } from "../routes/SessionView";
import { ThemeProvider } from "../theme/ThemeProvider";
import "./App.css";
import { useConfig } from "./config";
import { ConfigProvider } from "./ConfigContext";
import { SessionsProvider } from "./SessionsContext";
import { useSessionsStore } from "./sessionsStore";

// Lazy so @xyflow/react stays out of the main bundle until the overview is opened (#139).
const Overview = lazy(() => import("../routes/Overview"));
const SidebarOverview = lazy(() => import("../components/overview/SidebarOverview"));

const COLLAPSE_KEY = "tr-sidebar-collapsed";

type SidebarView = "list" | "overview";

/** Parse `/s/:engine/:id` from a pathname (the header lives above <Routes>, so useParams
 *  can't see the match — parse the path directly). Returns null off a session route. */
function parseSessionPath(pathname: string): { engine: string; id: string } | null {
  const m = /^\/s\/([^/]+)\/([^/]+)\/?$/.exec(pathname);
  return m ? { engine: decodeURIComponent(m[1]), id: decodeURIComponent(m[2]) } : null;
}

/** The current session's engine + title for the compact header. Engine comes from the
 *  URL (/s/:engine/:id); the title is resolved from the sidebar's loaded rows (shared via
 *  SessionsContext), falling back to a short id when the row isn't loaded yet. On the
 *  landing / settings routes there is no session → "New session". */
function CurrentSessionLabel() {
  const location = useLocation();
  const parsed = parseSessionPath(location.pathname);
  const { sessions } = useSessionsStore();
  if (!parsed) {
    return <span className="hdrTitle">New session</span>;
  }
  const { engine, id } = parsed;
  const row = sessions.find((s) => s.engine === engine && s.uuid === id);
  const title = row?.title || row?.first_user_message || `${id.slice(0, 8)}…`;
  return (
    <span className="hdrSession">
      <span className={`hdrBadge ${engine}`}>{engineBadge(engine)}</span>
      <span className="hdrEngine">{engineName(engine)}</span>
      <span className="hdrSep">·</span>
      <span className="hdrTitle" title={title}>
        {title}
      </span>
    </span>
  );
}

/** App shell: a session-list sidebar + a routed main pane.
 *  - Desktop (>800px): sidebar is a fixed 320px column spanning the full height; the thin
 *    top header sits only above the pane (it ends at the sidebar edge). While expanded, the
 *    sidebar is collapsed via its own .topbar PanelLeftClose icon — the header carries NO
 *    toggle then (one affordance, #132). When collapsed (persisted in localStorage) the pane
 *    goes full-width and the header spans it, showing an expand toggle + Settings gear.
 *  - Mobile (≤800px): sidebar is an off-canvas drawer toggled by the header hamburger; the
 *    pane is full-width. The drawer auto-closes after navigating.
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

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  // Sidebar body: session list or the squeezed Session Overview map (#139). Per-user,
  // persisted server-side like the theme; reconcile to the server value once config loads.
  const config = useConfig();
  const [sidebarView, setSidebarView] = useState<SidebarView>("list");
  const viewReconciled = useRef(false);
  useEffect(() => {
    if (viewReconciled.current || !config?.sidebar_view) return;
    viewReconciled.current = true;
    // One-time sync to the server-persisted value once /api/config loads (same pattern as
    // the theme reconcile + the drawer-close effect below).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (config.sidebar_view === "overview") setSidebarView("overview");
  }, [config?.sidebar_view]);
  const chooseView = (v: SidebarView) => {
    setSidebarView(v);
    api.setSidebarView(v).catch(() => {}); // best-effort; applies locally regardless
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

  const cls = ["app", navOpen ? "navOpen" : "", collapsed ? "collapsed" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={cls}>
      <header className="mobilebar">
        <button
          type="button"
          className="navToggle"
          aria-label="Toggle session list"
          aria-expanded={surfaceOpen}
          onClick={toggle}
        >
          {surfaceOpen ? <X size={18} /> : <Menu size={18} />}
        </button>
        <CurrentSessionLabel />
        <span className="mobilebarActions">
          <Link to="/overview" className="gear" aria-label="Open session overview">
            <Network size={18} />
          </Link>
          <Link to="/settings" className="gear" aria-label="Settings">
            <SettingsIcon size={18} />
          </Link>
        </span>
      </header>
      <aside className="sidebar">
        <header className="topbar">
          <span className="brand">
            👑 Term<b>Royale</b>
          </span>
          <span className="topbarActions">
            <Link to="/overview" className="gear" aria-label="Open session overview">
              <Network size={18} />
            </Link>
            <Link to="/settings" className="gear" aria-label="Settings">
              <SettingsIcon size={18} />
            </Link>
            <button
              type="button"
              className="gear collapseToggle"
              aria-label="Collapse session list"
              onClick={() => setCollapsed(true)}
            >
              <PanelLeftClose size={18} />
            </button>
          </span>
        </header>
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
            <Suspense fallback={<div className="tr-overview tr-ov-state">Loading map…</div>}>
              <SidebarOverview />
            </Suspense>
          ) : (
            <SessionList />
          )}
        </div>
      </aside>
      <button
        type="button"
        className="backdrop"
        aria-label="Close session list"
        tabIndex={-1}
        onClick={() => setNavOpen(false)}
      />
      <main className="terminal-pane">
        <Suspense fallback={<div className="tr-overview tr-ov-state">Loading…</div>}>
          <Routes>
            <Route path="/" element={<NewSessionLanding />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/s/:engine/:id" element={<SessionView />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ConfigProvider>
      <ThemeProvider>
        <SessionsProvider>
          <BrowserRouter>
            <Layout />
          </BrowserRouter>
        </SessionsProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
