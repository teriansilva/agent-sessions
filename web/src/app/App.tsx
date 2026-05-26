import { Menu, PanelLeftClose, Settings as SettingsIcon, X } from "lucide-react";
import { useEffect, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { SessionList } from "../components/sidebar/SessionList";
import { engineBadge, engineName } from "../lib/format";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { Settings } from "../routes/Settings";
import { SessionView } from "../routes/SessionView";
import { ThemeProvider } from "../theme/ThemeProvider";
import "./App.css";
import { ConfigProvider } from "./ConfigContext";
import { SessionsProvider } from "./SessionsContext";
import { useSessionsStore } from "./sessionsStore";

const COLLAPSE_KEY = "tr-sidebar-collapsed";

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
        <Link to="/settings" className="gear mobileGear" aria-label="Settings">
          <SettingsIcon size={18} />
        </Link>
      </header>
      <aside className="sidebar">
        <header className="topbar">
          <span className="brand">
            👑 Term<b>Royale</b>
          </span>
          <span className="topbarActions">
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
        <SessionList />
      </aside>
      <button
        type="button"
        className="backdrop"
        aria-label="Close session list"
        tabIndex={-1}
        onClick={() => setNavOpen(false)}
      />
      <main className="terminal-pane">
        <Routes>
          <Route path="/" element={<NewSessionLanding />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/s/:engine/:id" element={<SessionView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
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
