import { Menu, X } from "lucide-react";
import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { SessionList } from "../components/sidebar/SessionList";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { SessionView } from "../routes/SessionView";
import "./App.css";
import { ConfigProvider } from "./ConfigContext";

/** App shell: a session-list sidebar + a routed main pane.
 *  - Desktop (>800px): sidebar is a fixed 320px column beside the pane.
 *  - Mobile (≤800px): sidebar is an off-canvas drawer toggled by the top-bar
 *    hamburger; the pane is full-width. The drawer auto-closes after navigating
 *    (tapping a row) so a tap takes you straight to the session.
 *  The session lives in the URL (/s/:engine/:id); "/" is the new-session landing. */
function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  // Close the mobile drawer whenever the route changes (e.g. a row was tapped).
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNavOpen(false);
  }, [location.pathname]);

  return (
    <div className={navOpen ? "app navOpen" : "app"}>
      <header className="mobilebar">
        <button
          type="button"
          className="navToggle"
          aria-label="Toggle session list"
          aria-expanded={navOpen}
          onClick={() => setNavOpen((o) => !o)}
        >
          {navOpen ? <X size={18} /> : <Menu size={18} />}
        </button>
        <strong>agent-sessions</strong>
      </header>
      <aside className="sidebar">
        <header className="topbar">
          <strong>agent-sessions</strong>
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
      <BrowserRouter>
        <Layout />
      </BrowserRouter>
    </ConfigProvider>
  );
}
