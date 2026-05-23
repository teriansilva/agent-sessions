import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { SessionList } from "../components/sidebar/SessionList";
import { NewSessionLanding } from "../routes/NewSessionLanding";
import { SessionView } from "../routes/SessionView";
import "./App.css";
import { ConfigProvider } from "./ConfigContext";

/** App shell: persistent sidebar (session list) + a routed main pane.
 *  The session lives in the URL (/s/:engine/:id); "/" is the new-session landing.
 *  Filters, the real terminal, compose/nav land in later phases per #64. */
function Layout() {
  return (
    <div className="app">
      <aside className="sidebar">
        <header className="topbar">
          <strong>agent-sessions</strong>
        </header>
        <SessionList />
      </aside>
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
