// Browser entry for the Home Free connect page (loaded by connect.html). Thin
// DOM + xterm glue over the testable core in connect.ts. This page is standalone
// and NOT linked from the app — it exists for reaching a box through the relay.

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import "@xterm/xterm/css/xterm.css";

import {
  type AltchaChallenge,
  type SessionHandle,
  type SocketLike,
  ViewerError,
  relayUrls,
  runViewerSession,
  solveAltcha,
} from "./connect";
import { mountApp } from "./appMount";

// Full-app streaming (#579 P4b) is opt-in via `?app` for now — the default stays the
// recovery shell (the only path with a real-browser test until P7), so this can't regress
// the working page. P7 validates app mode end-to-end, then the default flips.
const appMode = new URLSearchParams(location.search).has("app");

const byId = (id: string): HTMLElement => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} missing`);
  return el;
};

const relayInput = byId("relay") as HTMLInputElement;
const nameInput = byId("name") as HTMLInputElement;
const keyInput = byId("key") as HTMLInputElement;
const connectBtn = byId("connect") as HTMLButtonElement;
const statusEl = byId("status");
const countdownEl = byId("countdown");
const termEl = byId("term");

function setStatus(text: string, kind: "info" | "error" | "ok" = "info"): void {
  statusEl.textContent = text;
  statusEl.dataset.kind = kind;
}

let countdownTimer: number | undefined;
function startCountdown(deadline: number): void {
  if (countdownTimer) window.clearInterval(countdownTimer);
  const tick = () => {
    const left = Math.max(0, Math.floor(deadline - Date.now() / 1000));
    const m = String(Math.floor(left / 60)).padStart(2, "0");
    const s = String(left % 60).padStart(2, "0");
    countdownEl.textContent = `${m}:${s}`;
    if (left <= 0 && countdownTimer) window.clearInterval(countdownTimer);
  };
  tick();
  countdownTimer = window.setInterval(tick, 1000);
}

async function connect(): Promise<void> {
  const base = relayInput.value.trim();
  const name = nameInput.value.trim();
  const key = keyInput.value.trim();
  if (!base || !name || !key) {
    setStatus("relay URL, console name and access key are required", "error");
    return;
  }

  connectBtn.disabled = true;

  if (appMode) {
    await connectApp(base, name, key);
    return;
  }

  const { altchaUrl, wsUrl } = relayUrls(base, name);

  const term = new Terminal({
    fontFamily: '"JetBrains Mono", ui-monospace, monospace',
    fontSize: 13,
    theme: { background: "#000000", foreground: "#e8e8e8", cursor: "#ffb000" },
    cursorBlink: true,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(termEl);
  fit.fit();
  window.addEventListener("resize", () => fit.fit());

  let handle: SessionHandle | null = null;
  const encoder = new TextEncoder();
  term.onData((d) => handle?.sendInput(encoder.encode(d)));

  try {
    setStatus("solving human verification…");
    const challenge = (await (await fetch(altchaUrl)).json()) as AltchaChallenge;
    const captcha = solveAltcha(challenge);

    setStatus("connecting to the relay…");
    const ws = new WebSocket(wsUrl);

    handle = await runViewerSession(ws as unknown as SocketLike, key, captcha, {
      onOutput: (bytes) => term.write(bytes),
      onEvent: (evt) => {
        if (evt.type === "paired") {
          setStatus("connected — end-to-end encrypted, the relay is blind", "ok");
          if (evt.deadline) startCountdown(evt.deadline);
        } else if (evt.type === "warn") {
          setStatus(`session ends soon (${evt.remaining ?? "<5m"}s) — reconnect after`, "info");
        } else if (evt.type === "expired") {
          setStatus("session expired (60-min limit) — reload to reconnect", "error");
        } else if (evt.type === "closed") {
          setStatus("disconnected", "error");
        }
      },
    });
  } catch (err) {
    const code = err instanceof ViewerError ? err.code : String(err);
    setStatus(`could not connect: ${code}`, "error");
    connectBtn.disabled = false;
  }
}

/** Full-app mode: solve the captcha, open the relay socket, then mount the real BattleLab SPA
 *  over the tunnel (`appMount`). On success the connect chrome collapses and #app-root fills
 *  the screen; on failure the error surfaces and the user can retry (or drop the `?app` to use
 *  the recovery shell). */
async function connectApp(base: string, name: string, key: string): Promise<void> {
  const { altchaUrl, wsUrl } = relayUrls(base, name);
  try {
    setStatus("solving human verification…");
    const challenge = (await (await fetch(altchaUrl)).json()) as AltchaChallenge;
    const captcha = solveAltcha(challenge);

    setStatus("connecting to the relay…");
    const ws = new WebSocket(wsUrl);
    await mountApp(ws as unknown as SocketLike, key, captcha, {
      onEvent: (evt) => {
        if (evt.type === "paired") {
          setStatus("connected — streaming your BattleLab, the relay is blind", "ok");
          if (evt.deadline) startCountdown(evt.deadline);
        } else if (evt.type === "expired") {
          setStatus("session expired (60-min limit) — reload to reconnect", "error");
        } else if (evt.type === "closed") {
          setStatus("disconnected", "error");
        }
      },
    });
    document.body.classList.add("app-mounted"); // collapse the connect chrome, reveal #app-root
  } catch (err) {
    const code = err instanceof ViewerError ? err.code : String(err);
    setStatus(`could not connect: ${code}`, "error");
    connectBtn.disabled = false;
  }
}

connectBtn.addEventListener("click", () => {
  void connect();
});
