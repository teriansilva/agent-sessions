import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./app/App.tsx";
import { bootTheme } from "./theme/applyTheme";
import { bootAccent } from "./theme/applyAccent";
import { initSWUpdates } from "./app/swUpdate";
import { HandoffStub } from "./components/links/HandoffStub";
import { isStandalone, peekBootEntry } from "./lib/bootEntry";
import { offerLink } from "./lib/linkHandoff";

// Re-apply the device-cached theme + accent in case the inline pre-paint script in
// index.html was stripped (e.g. a strict CSP). No-op when it already ran (same values).
bootTheme();
bootAccent();

// Explicit SW registration + update checks (#661) — replaces the injected registerSW.js
// (vite.config sets injectRegister: null) so a long-lived tab actually notices releases.
void initSWUpdates();

const root = createRoot(document.getElementById("root")!);
const renderApp = () =>
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );

// A tab opened ON a session or mission link first offers it to a BattleLab tab that is already
// open (#1232), and becomes a short "opened there" page when one takes it. The installed app
// skips this: `launch_handler` already routes the link into its existing window. The offer waits
// at most `HANDOFF_WAIT_MS`; with no other tab it falls through to booting here, as before.
const bootEntry = isStandalone() ? null : peekBootEntry();
if (bootEntry) {
  void offerLink(bootEntry).then((taken) => {
    if (!taken) return renderApp();
    root.render(
      <StrictMode>
        <HandoffStub entry={bootEntry} onOpenHere={renderApp} />
      </StrictMode>,
    );
  });
} else {
  renderApp();
}
