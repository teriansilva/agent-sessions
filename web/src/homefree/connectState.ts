// Presentational connect-page states (#579, NIGHTJAR #1596): connecting and the error card. Pure
// DOM (the connect page is vanilla, no framework); no network / mux logic lives here. It is
// framework-free and side-effect-free so it can be unit-tested.
//
// Design notes it enforces (docs/design.md): the chrome colour is the LED of the step in flight,
// kept SEPARATE from the status hues — green (`up`) for a step that finished, red (`down`)
// reserved for the step that failed.

export type StepState = "done" | "active" | "pending" | "failed";

export type ConnectStep = {
  label: string;
  state: StepState;
};

export type ConnectState =
  | { kind: "loading"; box?: string; steps?: ConnectStep[] }
  | { kind: "error"; box?: string; title: string; message: string; steps?: ConnectStep[] };

export interface ConnectActions {
  onRetry?: () => void;
  onBack?: () => void;
}

/** The four things a connect does, in order. `reached` counts the ones that finished. */
export const CONNECT_STEPS = ["Relay challenge", "Relay reached", "Box found", "Key exchange"] as const;

/** The steps with the first `reached` done, the next one `at` (active or failed), the rest pending. */
export function connectSteps(reached: number, at: "active" | "failed"): ConnectStep[] {
  return CONNECT_STEPS.map((label, i) => ({
    label,
    state: i < reached ? "done" : i === reached ? at : "pending",
  }));
}

/** What a failure code means to the person at the keyboard: the step it failed at, a title and a
 *  next move. Codes are the client's (`connect.ts`: captcha_unsolvable, connection_failed,
 *  protocol), the relay's (`battlelab-cloud relay/protocol.py` E_*), and `handshake` (the box and
 *  this browser disagree on the access password). An unknown code fails at the step in flight. */
const FAILURES: Record<string, { step: number; title: string; message: string }> = {
  captcha_unsolvable: {
    step: 0,
    title: "Relay challenge failed",
    message: "The relay's challenge couldn't be solved here. Retry; if it keeps failing, the relay may be updating.",
  },
  captcha_failed: {
    step: 1,
    title: "Verification refused",
    message: "The relay refused the challenge answer. Retry with a fresh one.",
  },
  connection_failed: {
    step: 1,
    title: "Relay unreachable",
    message: "Couldn't open a connection to the relay. Check your network, then retry.",
  },
  rate_limited: {
    step: 1,
    title: "Too many attempts",
    message: "The relay is limiting attempts from this network. Wait a minute, then retry.",
  },
  paused: {
    step: 1,
    title: "Relay paused",
    message: "The relay's operator has paused new connections. Try again later.",
  },
  banned: {
    step: 1,
    title: "Connection refused",
    message: "The relay refused this connection.",
  },
  bad_name: {
    step: 2,
    title: "Console key not valid",
    message: "A console key is lowercase letters, digits and dashes, like viper-8231. Check the one your install printed.",
  },
  offline: {
    step: 2,
    title: "Your box is offline",
    message: "No box is connected under this console key. Check the key, and that the box is on with BattleLab running, then retry.",
  },
  busy: {
    step: 2,
    title: "Your box is busy",
    message: "Another browser is connected to this box right now. Sign out there, or wait, then retry.",
  },
  agent_unreachable: {
    step: 2,
    title: "Your box didn't answer",
    message: "The box is registered but didn't answer the relay. Check that it's on and BattleLab is running, then retry.",
  },
  handshake: {
    step: 3,
    title: "Access password didn't match",
    message: "The key exchange failed: this access password isn't the box's. Check the password your install printed.",
  },
};

/** The error card for a failure `code`, raised after `reached` steps had finished. */
export function errorState(code: string, box: string, reached: number): ConnectState {
  const known = FAILURES[code];
  if (known) {
    return { kind: "error", box, title: known.title, message: known.message, steps: connectSteps(known.step, "failed") };
  }
  const step = Math.max(0, Math.min(reached, CONNECT_STEPS.length - 1));
  return {
    kind: "error",
    box,
    title: step === 0 ? "Relay unreachable" : "Could not connect",
    message:
      step === 0
        ? "The relay didn't answer. Check your network, or the custom relay URL, then retry."
        : `Something failed on the way to your box (${code}). Retry; if it keeps failing, check the console key and access password.`,
    steps: connectSteps(step, "failed"),
  };
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const STEP_LED: Record<StepState, string> = {
  done: "hud-led up",
  active: "hud-led",
  pending: "hud-led idle",
  failed: "hud-led down",
};

function stepList(steps: ConnectStep[]): HTMLElement {
  const list = el("ul", "cs-steps");
  for (const s of steps) {
    const li = el("li", `cs-step cs-step-${s.state}`);
    const led = el("span", STEP_LED[s.state]);
    led.setAttribute("aria-hidden", "true");
    // The state is in the words too, never only in the LED's colour.
    const word = s.state === "done" ? "done" : s.state === "failed" ? "failed" : s.state === "active" ? "in progress" : "waiting";
    li.append(led, el("span", "cs-step-label", s.label), el("span", "nj-sr-only", ` — ${word}`));
    list.append(li);
  }
  return list;
}

function button(cls: string, label: string, onClick?: () => void): HTMLButtonElement {
  const b = el("button", `${cls} nj-plate`, label);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  else b.disabled = true;
  return b;
}

function callsign(box: string | undefined): HTMLElement {
  const c = el("p", "nj-callsign cs-callsign");
  const notch = el("span", "nj-notch");
  notch.setAttribute("aria-hidden", "true");
  c.append(notch, el("span", undefined, "Home Free //"));
  if (box) c.append(el("span", "nj-value", box));
  return c;
}

/** Render `state` into `host`, replacing its contents. Idempotent — safe to call on every
 *  transition. `host.dataset.state` is set to the kind for styling/testing. */
export function renderConnectState(host: HTMLElement, state: ConnectState, actions: ConnectActions = {}): void {
  host.textContent = "";
  host.dataset.state = state.kind;

  if (state.kind === "loading") {
    const head = el("div", "cs-head");
    const radar = el("span", "nj-radar");
    radar.setAttribute("aria-hidden", "true");
    head.append(radar, el("h2", "cs-title", "Connecting"));
    host.append(callsign(state.box), head);
    if (state.steps?.length) host.append(stepList(state.steps));
    const bar = el("div", "nj-acquire cs-acquire");
    bar.setAttribute("aria-hidden", "true");
    host.append(bar, el("p", "cs-note", "End-to-end encrypted // the relay is blind"));
    return;
  }

  host.append(callsign(state.box), el("h2", "cs-title", state.title));
  if (state.steps?.length) host.append(stepList(state.steps));
  host.append(el("p", "cs-message", state.message));
  const row = el("div", "cs-actions");
  row.append(button("ghost", "Back", actions.onBack), button("primary", "Retry ▸", actions.onRetry));
  host.append(row);
}
