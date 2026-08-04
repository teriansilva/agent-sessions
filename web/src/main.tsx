import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./app/App.tsx";
import { bootTheme } from "./theme/applyTheme";
import { bootAccent } from "./theme/applyAccent";
import { initSWUpdates } from "./app/swUpdate";

// Re-apply the device-cached theme + accent in case the inline pre-paint script in
// index.html was stripped (e.g. a strict CSP). No-op when it already ran (same values).
bootTheme();
bootAccent();

// Explicit SW registration + update checks (#661) — replaces the injected registerSW.js
// (vite.config sets injectRegister: null) so a long-lived tab actually notices releases.
void initSWUpdates();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
