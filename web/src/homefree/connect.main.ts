// Browser entry for the Home Free connect page (loaded by connect.html). Thin DOM
// glue over the app-mode session core in connect.ts. This page is standalone and
// NOT linked from the app — it exists for reaching a box through the relay.

import {
  type AltchaChallenge,
  type SocketLike,
  ViewerError,
  relayUrls,
  solveAltcha,
} from "./connect";
import { mountApp } from "./appMount";

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

// On the public BattleLab deploy (battlelab.superstatus.io/connect), the relay is ours:
// prefill it and hide the field so the user only supplies name + key. Self-hosted copies keep
// the relay field editable. We deliberately do NOT read `?relay=`; a link-controlled relay
// default would make phishing the access key too easy.
const PUBLIC_RELAY = "https://relay.battlelab.superstatus.io";
if (
  location.hostname === "battlelab.superstatus.io" ||
  location.hostname.endsWith(".battlelab.superstatus.io")
) {
  relayInput.value = PUBLIC_RELAY;
  relayInput.readOnly = true;
  relayInput.hidden = true; // stays in the DOM so connect() still reads .value
}

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
  await connectApp(base, name, key);
}

/** Solve the captcha, open the relay socket, then mount the real BattleLab SPA over the tunnel. */
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

connectBtn.addEventListener("click", () => {
  void connect();
});
