// Browser entry for the Home Free connect page (loaded by connect.html). Thin DOM
// glue over the app-mode session core in connect.ts. Owns the public-page UI state,
// relay-deadline-derived sessionStorage credential restore (4-hour fallback), sign-out cleanup, and connected URL
// canonicalization. This page is standalone and NOT linked from the app — it exists
// for reaching a box through the relay.

import { runButtonGlitch, runDataFlow } from "../components/hud/dataFlow";
import {
  type AltchaChallenge,
  type SocketLike,
  ViewerError,
  relayUrls,
  solveAltcha,
} from "./connect";
import { type MountedApp, mountApp } from "./appMount";
import { sessionExpiredMessage, sessionExpiryMs } from "./sessionWindow";

type UiState = "signed-out" | "connecting" | "connected";

interface SavedConnectSession {
  relay: string;
  name: string;
  key: string;
  expiresAt: number;
}

interface ConnectHarness {
  makeWebSocket?: (url: string) => SocketLike;
  mountApp?: typeof mountApp;
  now?: () => number;
}

declare global {
  interface Window {
    __battlelabConnectHarness?: ConnectHarness;
  }
}

const PUBLIC_RELAY = "https://relay.battlelab.superstatus.io";
const STORAGE_KEY = "battlelab.connect.session.v1";

const byId = (id: string): HTMLElement => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} missing`);
  return el;
};

const body = document.body;
const bgCanvas = byId("bg") as HTMLCanvasElement;
const howModal = byId("how-modal") as HTMLDialogElement;
const howOpenBtn = byId("how-it-works") as HTMLButtonElement;
const howCloseBtn = byId("how-close") as HTMLButtonElement;
const form = byId("connect-form") as HTMLFormElement;
const relayInput = byId("relay") as HTMLInputElement;
const relayOptions = byId("relay-options") as HTMLDetailsElement;
const nameInput = byId("name") as HTMLInputElement;
const keyInput = byId("key") as HTMLInputElement;
const connectBtn = byId("connect") as HTMLButtonElement;
const signoutBtn = byId("signout") as HTMLButtonElement;
const statusEl = byId("status");
const sessionBox = byId("session-box");
const sessionNameEl = byId("session-name");
const sessionStatusEl = byId("session-status");
const countdownEl = byId("countdown");
const sessionToggle = byId("session-toggle") as HTMLButtonElement;
const sessionLed = sessionBox.querySelector<HTMLElement>(".session-led");

function harness(): ConnectHarness | undefined {
  return window.__battlelabConnectHarness;
}

function nowMs(): number {
  return harness()?.now?.() ?? Date.now();
}

function isPublicDeploy(): boolean {
  return (
    location.hostname === "battlelab.superstatus.io" ||
    location.hostname.endsWith(".battlelab.superstatus.io")
  );
}

// The ambient HUD backdrop + button glitch, shared with the SPA (components/hud/dataFlow).
// Both are torn down once the streamed app takes the screen — it mounts its own pair.
let stopDataFlow: (() => void) | null = null;
let stopGlitch: (() => void) | null = null;

function setAmbient(on: boolean): void {
  if (on && !stopDataFlow) {
    stopDataFlow = runDataFlow(bgCanvas);
    stopGlitch = runButtonGlitch();
    return;
  }
  if (!on) {
    stopDataFlow?.();
    stopGlitch?.();
    stopDataFlow = null;
    stopGlitch = null;
  }
}

function setState(state: UiState): void {
  body.dataset.state = state;
  connectBtn.disabled = state === "connecting";
  sessionBox.hidden = state !== "connected";
  setAmbient(state !== "connected");
  if (state === "connected") {
    setSessionLed("up");
    applyBarDefault(); // pick collapsed/expanded for this viewport (unless the user chose)
  }
}

function setStatus(text: string, kind: "info" | "error" | "ok" = "info"): void {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
  sessionStatusEl.textContent = text;
}

// Collapsible connection bar (#684). On small screens the full bar overlaps the app toolbar, so
// it collapses to just the status LED — a 44px toggle that lets toolbar taps pass through. A
// manual choice is remembered for the session and wins over viewport changes; with no choice,
// narrow viewports (≤800px) default to collapsed.
const BAR_KEY = "battlelab.connect.bar.v1";
let barCollapsed = false;
// An explicit user choice this page view, tracked in memory independently of sessionStorage —
// so the choice still wins over viewport changes even when storage is blocked (setItem throws).
let barOverridden = false;

// An explicit choice exists if the user toggled this view OR a prior choice is in storage.
function hasBarOverride(): boolean {
  return barOverridden || readBarOverride() !== null;
}

function readBarOverride(): boolean | null {
  try {
    const v = sessionStorage.getItem(BAR_KEY);
    if (v === "collapsed") return true;
    if (v === "expanded") return false;
  } catch {
    /* storage disabled — treat as no override */
  }
  return null; // no or unrecognised value → fall back to the viewport default
}

function updateToggleLabel(): void {
  const cd = countdownEl.textContent;
  const time = cd && cd !== "--:--" ? `, ${cd} left` : "";
  const name = sessionNameEl.textContent || "session";
  const label = `${barCollapsed ? "Expand" : "Collapse"} connection bar — ${name}${time}`;
  sessionToggle.setAttribute("aria-label", label);
  sessionToggle.title = label;
}

function renderBar(): void {
  sessionBox.classList.toggle("collapsed", barCollapsed);
  sessionToggle.setAttribute("aria-expanded", String(!barCollapsed));
  updateToggleLabel();
}

function setBarCollapsed(collapsed: boolean, persist: boolean): void {
  barCollapsed = collapsed;
  if (persist) {
    barOverridden = true; // an explicit choice — remembered in memory regardless of storage
    try {
      sessionStorage.setItem(BAR_KEY, collapsed ? "collapsed" : "expanded");
    } catch {
      /* storage disabled — the in-memory choice still applies for this view */
    }
  }
  renderBar();
}

const barNarrowMq = window.matchMedia("(max-width: 800px)");
function applyBarDefault(): void {
  // The current view's explicit choice is authoritative — even over a stale-but-readable stored
  // value (blocked writes leave old storage in place, so it must not overwrite the newer choice).
  if (barOverridden) {
    renderBar();
    return;
  }
  const stored = readBarOverride();
  setBarCollapsed(stored ?? barNarrowMq.matches, false); // else a stored choice, else viewport
}
// Follow viewport changes (resize / rotate across the 800px breakpoint) while there is no
// explicit user choice — a stored OR in-memory collapsed/expanded stays authoritative.
barNarrowMq.addEventListener("change", () => {
  if (!hasBarOverride()) setBarCollapsed(barNarrowMq.matches, false);
});

function setSessionLed(kind: "up" | "warn" | "down"): void {
  if (sessionLed) sessionLed.className = kind === "up" ? "session-led" : `session-led ${kind}`;
}

sessionToggle.addEventListener("click", () => setBarCollapsed(!barCollapsed, true));

let countdownTimer: number | undefined;
let expiryTimer: number | undefined;
let activeApp: MountedApp | null = null;

function stopTimer(timer: number | undefined): undefined {
  if (timer) window.clearTimeout(timer);
  return undefined;
}

function stopCountdown(): void {
  if (countdownTimer) window.clearInterval(countdownTimer);
  countdownTimer = undefined;
  countdownEl.textContent = "--:--";
}

function clearSavedSession(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private-mode or storage-disabled browsers still get a working one-shot session.
  }
  expiryTimer = stopTimer(expiryTimer);
}

function validSavedSession(value: unknown): value is SavedConnectSession {
  if (!value || typeof value !== "object") return false;
  const v = value as Partial<SavedConnectSession>;
  return (
    typeof v.relay === "string" &&
    typeof v.name === "string" &&
    typeof v.key === "string" &&
    typeof v.expiresAt === "number" &&
    Number.isFinite(v.expiresAt)
  );
}

function readSavedSession(): SavedConnectSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const saved: unknown = JSON.parse(raw);
    if (!validSavedSession(saved) || saved.expiresAt <= nowMs()) {
      clearSavedSession();
      return null;
    }
    return saved;
  } catch {
    clearSavedSession();
    return null;
  }
}

function scheduleStorageExpiry(expiresAt: number): void {
  expiryTimer = stopTimer(expiryTimer);
  const delay = Math.max(0, expiresAt - nowMs());
  expiryTimer = window.setTimeout(() => endSession(sessionExpiredMessage(), "expired"), delay);
}

function saveSession(base: string, name: string, key: string, deadline?: number): void {
  const expiresAt = sessionExpiryMs(nowMs(), deadline);
  if (!Number.isFinite(expiresAt) || expiresAt <= nowMs()) return;
  const saved: SavedConnectSession = { relay: base, name, key, expiresAt };
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
    scheduleStorageExpiry(expiresAt);
  } catch {
    // Storage is a convenience, not a connection prerequisite.
  }
}

function canonicalConnectPath(): string {
  if (isPublicDeploy() || location.pathname === "/connect" || location.pathname.startsWith("/connect/")) {
    return "/connect/";
  }
  return "/connect.html";
}

function canonicalizeConnectedUrl(): void {
  const target = canonicalConnectPath();
  if (`${location.pathname}${location.search}${location.hash}` !== target) {
    history.replaceState(null, "", target);
  }
}

function startCountdown(deadline: number): void {
  stopCountdown();
  const tick = () => {
    const left = Math.max(0, Math.floor(deadline - Date.now() / 1000));
    const m = String(Math.floor(left / 60)).padStart(2, "0");
    const s = String(left % 60).padStart(2, "0");
    countdownEl.textContent = `${m}:${s}`;
    updateToggleLabel(); // keep the collapsed control's accessible name/tooltip current
    if (left <= 0) endSession(sessionExpiredMessage(), "expired");
  };
  tick();
  countdownTimer = window.setInterval(tick, 1000);
}

function resetForm(clearIdentity = false): void {
  if (clearIdentity) {
    nameInput.value = "";
    keyInput.value = "";
  }
  connectBtn.disabled = false;
  setState("signed-out");
}

function teardownActiveApp(): void {
  activeApp?.teardown();
  activeApp = null;
}

function endSession(message: string, reason: "expired" | "closed" | "signed-out"): void {
  teardownActiveApp();
  clearSavedSession();
  stopCountdown();
  if (reason === "signed-out") {
    setStatus("signed out — saved credentials cleared");
    resetForm(true);
    return;
  }
  setStatus(message, "error");
  resetForm(reason === "expired");
}

function applyPublicRelayDefault(): void {
  if (!isPublicDeploy()) return;
  relayInput.value = PUBLIC_RELAY;
  relayInput.readOnly = true;
  relayOptions.hidden = true;
}

function restoreSavedSession(): SavedConnectSession | null {
  const saved = readSavedSession();
  if (!saved) return null;

  relayInput.value = isPublicDeploy() ? PUBLIC_RELAY : saved.relay;
  nameInput.value = saved.name;
  keyInput.value = saved.key;
  scheduleStorageExpiry(saved.expiresAt);
  return saved;
}

async function connect(): Promise<void> {
  const base = (relayInput.value.trim() || (isPublicDeploy() ? PUBLIC_RELAY : "")).replace(/\/+$/, "");
  const name = nameInput.value.trim();
  const key = keyInput.value.trim();
  if (!base || !name || !key) {
    setStatus("console key and access password are required", "error");
    return;
  }

  setState("connecting");
  sessionNameEl.textContent = `Connecting to ${name}`;
  setStatus("solving human verification…");

  try {
    activeApp = await connectApp(base, name, key);
  } catch (err) {
    const code = err instanceof ViewerError ? err.code : String(err);
    teardownActiveApp();
    setStatus(`could not connect: ${code}`, "error");
    resetForm();
  }
}

/** Solve the captcha, open the relay socket, then mount the real BattleLab SPA over the tunnel. */
async function connectApp(base: string, name: string, key: string): Promise<MountedApp> {
  const { altchaUrl, wsUrl } = relayUrls(base, name);
  const fetchFn = window.fetch.bind(window);
  const makeWebSocket = harness()?.makeWebSocket ?? ((url: string) => new WebSocket(url) as unknown as SocketLike);
  const mount = harness()?.mountApp ?? mountApp;

  const challenge = (await (await fetchFn(altchaUrl)).json()) as AltchaChallenge;
  const captcha = solveAltcha(challenge);
  setStatus("connecting to the relay…");

  const ws = makeWebSocket(wsUrl);
  return mount(ws, key, captcha, {
    onEvent: (evt) => {
      if (evt.type === "paired") {
        canonicalizeConnectedUrl();
        sessionNameEl.textContent = `Connected to ${name}`;
        setState("connected");
        setStatus("connected — streaming your BattleLab, the relay is blind", "ok");
        saveSession(base, name, key, evt.deadline);
        if (evt.deadline) startCountdown(evt.deadline);
      } else if (evt.type === "warn") {
        setStatus(`session ends soon (${evt.remaining ?? "<5m"}s) — reconnect after`);
        setSessionLed("warn"); // amber pulse so a collapsed bar still signals near-expiry
        updateToggleLabel();
      } else if (evt.type === "expired") {
        endSession(sessionExpiredMessage(), "expired");
      } else if (evt.type === "closed") {
        endSession("disconnected", "closed");
      }
    },
  });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void connect();
});

signoutBtn.addEventListener("click", () => {
  endSession("signed out", "signed-out");
});

// "How does it work?" — what the relay can and cannot see. <dialog> gives us the focus trap,
// Esc-to-close and inert background for free; the only extra is click-outside-to-dismiss.
howOpenBtn.addEventListener("click", () => howModal.showModal());
howCloseBtn.addEventListener("click", () => howModal.close());
howModal.addEventListener("click", (event) => {
  if (event.target === howModal) howModal.close(); // the backdrop, not the panel
});

setAmbient(true);
applyPublicRelayDefault();
const restored = restoreSavedSession();
if (restored) {
  setStatus("restoring saved connect session…");
  void connect();
}
