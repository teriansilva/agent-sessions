// Browser entry for the Home Free connect page (loaded by connect.html). Thin DOM
// glue over the app-mode session core in connect.ts. Owns the public-page UI state,
// the single-use human-verification gate (#690 — every attempt, including a reload with
// restored credentials, requires a fresh press-and-hold before any network side effect),
// relay-deadline-derived sessionStorage credential restore (4-hour fallback), sign-out cleanup, and connected URL
// canonicalization. This page is standalone and NOT linked from the app — it exists
// for reaching a box through the relay.

// The app's own sheets, not a copy (#1596): index.css carries the palette, the faces, the motion
// modes and the NIGHTJAR primitives (appMount.ts needs it for the streamed app anyway), then the
// page's layout on them. Imported FIRST so connect.css comes after the primitives it composes
// (a plate's `--nj-face` set here must override nightjar.css's default, not lose to it). Vite links
// both in the page's <head>, so the card paints before any script runs.
import "../index.css";
import "./connect.css";

import { runButtonGlitch } from "../components/hud/dataFlow";
import { bootPageTheme } from "../theme/applyTheme";
import { applyMotion, prefersReducedMotion, resolveMotion, startMotionPause } from "../theme/motion";
import {
  type AltchaChallenge,
  type SocketLike,
  ViewerError,
  relayUrls,
  solveAltcha,
} from "./connect";
import { type MountedApp, mountApp, renderRealApp } from "./appMount";
import { CONNECT_STEPS, connectSteps, errorState, renderConnectState } from "./connectState";
import {
  type RelayLink,
  clearRelay,
  onRelayDockChange,
  publishRelay,
  relayPlateDocked,
  updateRelay,
} from "./relayStatus";
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
  /** Human-gate hold duration override (#690) — tests shorten it; production uses the default. */
  holdMs?: number;
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
const card = byId("connect-title").closest<HTMLElement>(".connect-card")!;
const connectView = byId("connect-view");
const progressHost = byId("connect-progress");
const errorHost = byId("connect-error");
const uplinkLabel = byId("uplink-label");
const uplinkLed = card.querySelector<HTMLElement>(".link-led");
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
const gateEl = byId("verify-gate");
const gateHoldBtn = byId("gate-hold") as HTMLButtonElement;
const gateHoldLabel = byId("gate-hold-label");
const gateMeter = byId("gate-meter");
const gateFill = byId("gate-fill");
const gateStateLabel = byId("gate-state-label");
const gatePct = byId("gate-pct");

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

// The theme (#1596): the device's stored BattleLab choice, else its colour scheme. The motion mode
// (#1531): this public page has no stored setting, so it follows the device's
// prefers-reduced-motion. Both are set before the ambient loops start and before the SPA streams
// in, whose stylesheets key every theme and reduced rule off `data-theme` / `data-motion`.
bootPageTheme();
applyMotion(resolveMotion("system", prefersReducedMotion()));

// The ambient: the atmosphere in the markup (`.nj-atmo`, CSS only — it follows `data-motion` and
// `data-motion-paused` by itself) and the button glitch, shared with the SPA
// (components/hud/dataFlow). The glitch is torn down once the streamed app takes the screen — it
// mounts its own.
let stopGlitch: (() => void) | null = null;
// This page owns the hidden-tab / idle pause (#1531) while its own ambient pair runs; once the
// streamed app takes the screen, the app's MotionProvider owns it.
let stopPause: (() => void) | null = null;

function setAmbient(on: boolean): void {
  if (on && !stopGlitch) {
    stopPause = startMotionPause();
    stopGlitch = runButtonGlitch();
    return;
  }
  if (!on) {
    stopGlitch?.();
    stopPause?.();
    stopGlitch = null;
    stopPause = null;
  }
}

// What the card shows (#1596): the form, the connecting steps, or the error card with Back and
// Retry. The uplink tag in the card's head says the same in words.
type CardView = "form" | "progress" | "error";
const UPLINK: Record<CardView, [string, string]> = {
  form: ["hud-led idle link-led", "Uplink // Offline"],
  progress: ["hud-led link-led", "Uplink // Acquiring"],
  error: ["hud-led down link-led", "Uplink // Failed"],
};

function showView(view: CardView): void {
  card.dataset.view = view;
  connectView.hidden = view !== "form";
  progressHost.hidden = view !== "progress";
  errorHost.hidden = view !== "error";
  if (view !== "progress") progressHost.textContent = "";
  if (view !== "error") errorHost.textContent = "";
  if (uplinkLed) uplinkLed.className = UPLINK[view][0];
  uplinkLabel.textContent = UPLINK[view][1];
}

// How far the attempt in flight got: the challenge, the relay, the box, the key exchange.
let reached = 0;
let attemptBox = "";

function showProgress(): void {
  showView("progress");
  renderConnectState(progressHost, { kind: "loading", box: attemptBox, steps: connectSteps(reached, "active") });
}

/** Back to the form after a failure. Retry lands on the gate (the next attempt needs a fresh
 *  hold, #690); Back lands on the console key, to change what was entered. */
function leaveError(to: "gate" | "key"): void {
  showView("form");
  if (to === "gate") {
    setStatus("hold to verify you're human, then press Connect to retry");
    gateHoldBtn.focus();
  } else {
    setStatus("Enter your console key and access password, then hold to verify you're human.");
    nameInput.focus();
  }
}

function setState(state: UiState): void {
  body.dataset.state = state;
  syncConnectEnabled();
  armBarFallback(state === "connected");
  setAmbient(state !== "connected");
  if (state === "connected") {
    setSessionLed("up");
    applyBarDefault(); // pick collapsed/expanded for this viewport (unless the user chose)
  }
}

// The floating bar is the FALLBACK (#1658). Connected, the streamed app's top bar shows the
// connection as a plate (components/shell/RelayChip, through homefree/relayStatus) and this bar
// stands down. It shows only while no plate is docked: 2 s after `connected` if none has docked
// yet (no flash while the app's bar renders), and at once if a docked plate leaves while still
// connected — so Sign out is reachable whatever the app renders.
const BAR_FALLBACK_MS = 2000;
let barFallbackDue = false;
let barFallbackTimer: number | undefined;

function syncSessionBox(): void {
  sessionBox.hidden =
    body.dataset.state !== "connected" || relayPlateDocked() || !barFallbackDue;
}

function armBarFallback(connected: boolean): void {
  barFallbackTimer = stopTimer(barFallbackTimer);
  barFallbackDue = false;
  if (connected)
    barFallbackTimer = window.setTimeout(() => {
      barFallbackTimer = undefined;
      barFallbackDue = true;
      syncSessionBox();
    }, BAR_FALLBACK_MS);
  syncSessionBox();
}

onRelayDockChange((docked) => {
  if (!docked && body.dataset.state === "connected") barFallbackDue = true;
  syncSessionBox();
});

// ── Human-verification gate (#690) ─────────────────────────────────────────────
// Connecting requires a deliberate press-and-hold gesture first. This is a browser-side
// deterrent against scripted/agent use of the connect page, NOT server-verifiable proof of
// humanity — the relay cannot tell the difference; a provider-backed check (e.g. Turnstile)
// can replace this widget later via the same reset()/consume() seam. Verification is
// single-use: consume() succeeds once, and every error/close/expiry re-gates the next attempt.

const HOLD_MS_DEFAULT = 1500;

type GateState = "idle" | "holding" | "verified";
let gateState: GateState = "idle";
let holdStartedAt = 0; // performance.now() timestamp — monotonic, independent of harness now()
let holdTimer: number | undefined;
let holdKeyDown = false; // a genuine sustained keydown; auto-repeat must not re-trigger

function gateHoldMs(): number {
  return harness()?.holdMs ?? HOLD_MS_DEFAULT;
}

function gateVerified(): boolean {
  return gateState === "verified";
}

function syncConnectEnabled(): void {
  connectBtn.disabled = body.dataset.state === "connecting" || !gateVerified();
}

function renderGate(pct: number): void {
  gateEl.dataset.state = gateState;
  gateFill.style.width = `${pct}%`;
  gateMeter.setAttribute("aria-valuenow", String(Math.round(pct)));
  gatePct.textContent = `${Math.round(pct)}%`;
  // Not a toggle (activation is one-way), so no aria-pressed: completion is
  // announced by the progressbar/status text, and a verified control is no
  // longer actionable — disable it until the verification is consumed.
  gateHoldBtn.disabled = gateState === "verified";
  const label =
    gateState === "verified"
      ? "Verified — human"
      : gateState === "holding"
        ? "Verifying…"
        : "Hold to verify";
  gateStateLabel.textContent = label;
  gateHoldLabel.textContent =
    gateState === "verified" ? "Verified — human" : "Hold to verify";
  syncConnectEnabled();
}

function stopHoldTimer(): void {
  if (holdTimer) window.clearInterval(holdTimer);
  holdTimer = undefined;
}

function gateTick(): void {
  const pct = Math.min(
    100,
    ((performance.now() - holdStartedAt) / gateHoldMs()) * 100,
  );
  if (pct >= 100) {
    stopHoldTimer();
    gateState = "verified";
    renderGate(100);
    setStatus("verified — press Connect to continue", "ok");
    return;
  }
  renderGate(pct);
}

function startHold(): void {
  if (gateState !== "idle") return;
  gateState = "holding";
  holdStartedAt = performance.now();
  renderGate(0);
  // An interval, not requestAnimationFrame: the meter is state, not decoration, so it must
  // keep working under prefers-reduced-motion (where this page starts zero rAF loops).
  holdTimer = window.setInterval(gateTick, 50);
}

// Any interruption of the sustained press is an explicit, honest transition back to idle.
// The key latch is cleared unconditionally: a cancellation whose keyup lands elsewhere
// (blur, hidden tab, focus transfer) must never leave future keyboard holds ignored.
function cancelHold(): void {
  holdKeyDown = false;
  if (gateState !== "holding") return;
  stopHoldTimer();
  gateState = "idle";
  renderGate(0);
}

function resetGate(): void {
  stopHoldTimer();
  holdKeyDown = false;
  gateState = "idle";
  renderGate(0);
}

/** Single-use: succeeds exactly once per completed hold, then re-gates. */
function consumeVerification(): boolean {
  if (gateState !== "verified") return false;
  resetGate();
  return true;
}

gateHoldBtn.addEventListener("pointerdown", (event) => {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  startHold();
});
gateHoldBtn.addEventListener("pointerup", cancelHold);
gateHoldBtn.addEventListener("pointercancel", cancelHold);
gateHoldBtn.addEventListener("pointerleave", cancelHold);
// Long-press on touch would otherwise open the context menu mid-hold.
gateHoldBtn.addEventListener("contextmenu", (event) => event.preventDefault());
gateHoldBtn.addEventListener("keydown", (event) => {
  if (event.key !== " " && event.key !== "Enter") return;
  event.preventDefault(); // no scroll-on-space, no implicit submit
  if (event.repeat || holdKeyDown) return; // OS auto-repeat is not a sustained press
  holdKeyDown = true;
  startHold();
});
// The release can land anywhere (focus may have moved mid-hold), so a keyboard-initiated
// hold listens for its keyup globally — the button-scoped listener would miss it and the
// timer would run on to "verified" after the physical key was already released.
window.addEventListener("keyup", (event) => {
  if (event.key !== " " && event.key !== "Enter") return;
  if (!holdKeyDown) return; // not a gate-initiated key hold — ignore typing elsewhere
  cancelHold();
});
// Focus leaving the control mid-hold is an interruption too (Tab-away with the key down).
gateHoldBtn.addEventListener("focusout", cancelHold);
window.addEventListener("blur", cancelHold);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) cancelHold();
});

function setStatus(text: string, kind: "info" | "error" | "ok" = "info"): void {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
  sessionStatusEl.textContent = text;
  updateRelay({ status: text }); // the plate's panel says the same (a no-op until published)
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
  if (sessionLed)
    sessionLed.className =
      kind === "up" ? "session-led hud-led" : `session-led hud-led ${kind}`;
}

sessionToggle.addEventListener("click", () =>
  setBarCollapsed(!barCollapsed, true),
);

let countdownTimer: number | undefined;
let expiryTimer: number | undefined;

/** One connect attempt, from Connect to its end (#1601 review 6800). Every exit — sign out, a relay
 *  close or expiry, the saved window running out, a newer attempt — aborts it; whatever the attempt
 *  still had in flight checks `signal` when it lands, so a mount that resolves after its attempt
 *  ended is torn down on arrival and assigns nothing. */
interface Attempt {
  ctl: AbortController;
  app: MountedApp | null;
  /** Settles once the attempt's own connect() has finished — including tearing down a late mount. */
  settled: Promise<void>;
  /** The attempt's link (#1658). `warn` can land while the app is still mounting, before anything
   *  is published: the first snapshot must carry it, and the post-mount status must not undo it. */
  link: RelayLink;
}
let attempt: Attempt | null = null;
/** The newest attempt's `settled` — kept after the attempt ends, so the next one can wait for it. */
let lastSettled: Promise<void> = Promise.resolve();

/** Run `fn` at most once — a teardown reached from two exits closes the tunnel once. */
function once(fn: () => void): () => void {
  let done = false;
  return () => {
    if (done) return;
    done = true;
    fn();
  };
}

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
  expiryTimer = window.setTimeout(
    () => endSession(sessionExpiredMessage(), "expired"),
    delay,
  );
}

function saveSession(
  base: string,
  name: string,
  key: string,
  deadline?: number,
): void {
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
  if (
    isPublicDeploy() ||
    location.pathname === "/connect" ||
    location.pathname.startsWith("/connect/")
  ) {
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
    // H:MM:SS for the 4-hour window (#1596: "234:59" read as minutes), MM:SS in the last hour.
    const h = Math.floor(left / 3600);
    const m = String(Math.floor((left % 3600) / 60)).padStart(2, "0");
    const s = String(left % 60).padStart(2, "0");
    countdownEl.textContent = h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
    updateToggleLabel(); // keep the collapsed control's accessible name/tooltip current
    updateRelay({ remaining: countdownEl.textContent });
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
  resetGate(); // every error/close/expiry/sign-out re-gates the next attempt (#690)
  showView("form");
  setState("signed-out");
}

/** End the attempt in flight, if any: abort it (closing its relay socket) and tear its app down.
 *  Idempotent — every exit path calls it, and a second call finds nothing to end. */
function endAttempt(): void {
  clearRelay(); // the plate goes with the attempt; a stale one can sign nothing out
  const a = attempt;
  attempt = null;
  if (!a) return;
  a.ctl.abort();
  const app = a.app;
  a.app = null;
  app?.teardown();
}

function endSession(
  message: string,
  reason: "expired" | "closed" | "signed-out",
): void {
  endAttempt();
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
  const base = (
    relayInput.value.trim() || (isPublicDeploy() ? PUBLIC_RELAY : "")
  ).replace(/\/+$/, "");
  const name = nameInput.value.trim();
  const key = keyInput.value.trim();
  if (!base || !name || !key) {
    setStatus("console key and access password are required", "error");
    return;
  }
  // Consume only after local validation, immediately before the first network side effect —
  // an invalid form must not waste the single-use verification, and a double submit
  // cannot reuse it. Nothing below may touch the network unless this succeeds.
  if (!consumeVerification()) {
    setStatus("hold to verify you're human, then press Connect", "error");
    return;
  }

  // A newer attempt supersedes any still in flight, and waits for the previous one — even one that
  // already ended but whose mount is still out — to land (and be torn down) before mounting: two
  // apps never share the tunnel seams or #app-root.
  const previous = lastSettled;
  endAttempt();
  const ctl = new AbortController();
  let settle!: () => void;
  const mine: Attempt = {
    ctl,
    app: null,
    settled: new Promise<void>((r) => (settle = r)),
    link: "linked",
  };
  attempt = mine;
  lastSettled = mine.settled;

  setState("connecting");
  sessionNameEl.textContent = `Connecting to ${name}`;
  setStatus("solving the relay challenge…");
  reached = 0;
  attemptBox = name;
  showProgress();

  try {
    await previous;
    if (ctl.signal.aborted) return;
    const { app, deadline } = await connectApp(base, name, key, ctl.signal);
    const teardown = once(app.teardown);
    if (ctl.signal.aborted) {
      // The attempt ended (sign out, close, expiry, a newer attempt) while the app was still
      // mounting: nothing may keep its tunnel. Tear it down now; assign nothing.
      teardown();
      return;
    }
    mine.app = { teardown };
    // Live only now the app is mounted (#1601 review 6800): until here the card holds the screen,
    // so there is no empty app and no Sign out to race the mount.
    reached = CONNECT_STEPS.length;
    canonicalizeConnectedUrl();
    sessionNameEl.textContent = `Connected to ${name}`;
    setState("connected");
    // An early `warn` (it landed while the app was mounting) keeps its amber LED and its "ends
    // soon" status: the panel must never pair "ends soon" with a "connected" line.
    if (mine.link === "ending") setSessionLed("warn");
    else setStatus("connected — streaming your BattleLab, the relay is blind", "ok");
    saveSession(base, name, key, deadline);
    if (deadline) startCountdown(deadline);
    // The countdown's first tick can already end the attempt (a deadline in the past): then there
    // is nothing to publish.
    if (ctl.signal.aborted) return;
    // Published only now — after the status decision — so the plate's first paint is right.
    publishRelay(
      {
        link: mine.link,
        name,
        status: sessionStatusEl.textContent ?? "",
        remaining: deadline ? (countdownEl.textContent ?? "") : "",
      },
      () => endSession("signed out", "signed-out"),
    );
    updateToggleLabel();
  } catch (err) {
    // A failure that lands after its attempt ended belongs to nobody: the page has moved on.
    if (ctl.signal.aborted) return;
    const code =
      err instanceof ViewerError
        ? err.code
        : err instanceof Error && err.name === "HandshakeError"
          ? "handshake"
          : String(err);
    endAttempt();
    setStatus(`could not connect: ${code}`, "error");
    resetForm();
    // The error card (#1596): which step failed, what it means, and Back / Retry.
    showView("error");
    renderConnectState(errorHost, errorState(code, name, reached), {
      onBack: () => leaveError("key"),
      onRetry: () => leaveError("gate"),
    });
    errorHost.querySelector<HTMLButtonElement>(".primary")?.focus();
  } finally {
    settle();
  }
}

/** Solve the captcha, open the relay socket, then mount the real BattleLab SPA over the tunnel.
 *  Every event is fenced by `signal`: once the attempt ends, its socket is closed and nothing it
 *  reports reaches the page. */
async function connectApp(
  base: string,
  name: string,
  key: string,
  signal: AbortSignal,
): Promise<{ app: MountedApp; deadline: number | undefined }> {
  const { altchaUrl, wsUrl } = relayUrls(base, name);
  const fetchFn = window.fetch.bind(window);
  const makeWebSocket =
    harness()?.makeWebSocket ??
    ((url: string) => new WebSocket(url) as unknown as SocketLike);
  const mount = harness()?.mountApp ?? mountApp;

  const challenge = (await (
    await fetchFn(altchaUrl, { signal })
  ).json()) as AltchaChallenge;
  signal.throwIfAborted();
  const captcha = solveAltcha(challenge);
  setStatus("connecting to the relay…");
  reached = 1;
  showProgress();

  const ws = makeWebSocket(wsUrl);
  // Ending the attempt closes its relay socket at once — a handshake or mount still in flight
  // cannot keep a live tunnel behind a signed-out page.
  signal.addEventListener("abort", () => ws.close(), { once: true });
  // The card renders every step as it happens (#1596): the socket opens (the relay is reached,
  // the box is next), the relay pairs the box (the key exchange is next), the key exchange
  // finishes (the app streams in next) — and only a mounted app takes the screen.
  let deadline: number | undefined;
  // `render` is the real SPA render, the real mountApp's own default; handed over explicitly so a
  // test harness standing in for the tunnel can still mount the real app (#1658).
  const app = await mount(ws, key, captcha, {
    render: renderRealApp,
    onEvent: (evt) => {
      if (signal.aborted) return; // a late event of an attempt that already ended
      if (evt.type === "open") {
        reached = 2; // the relay answered; it looks for the box next
        setStatus("relay reached — finding your box…");
        showProgress();
      } else if (evt.type === "paired") {
        reached = 3; // the relay found the box; the key exchange runs next
        deadline = evt.deadline;
        setStatus("box found — exchanging keys…");
        showProgress();
      } else if (evt.type === "secured") {
        reached = CONNECT_STEPS.length; // every step done; the app is streaming in
        setStatus("keys exchanged — loading your BattleLab…");
        showProgress();
      } else if (evt.type === "warn") {
        // Events are fenced by `signal`, so the attempt in flight is the one this event belongs to.
        if (attempt) attempt.link = "ending";
        setStatus(
          `session ends soon (${evt.remaining ?? "<5m"}s) — reconnect after`,
        );
        setSessionLed("warn"); // amber pulse so a collapsed bar still signals near-expiry
        updateRelay({ link: "ending" });
        updateToggleLabel();
      } else if (evt.type === "expired") {
        endSession(sessionExpiredMessage(), "expired");
      } else if (evt.type === "closed") {
        endSession("disconnected", "closed");
      }
    },
  });
  return { app, deadline };
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
renderGate(0); // Connect starts disabled until a human verifies (#690)
const restored = restoreSavedSession();
if (restored) {
  // Saved credentials prefill the form but never auto-connect: the gate is per-attempt, so
  // a reload must stop at a fresh unverified gate — no ALTCHA fetch, no WebSocket.
  setStatus(
    "session restored — hold to verify you're human, then press Connect",
  );
}
