// Presentational connect-page states (#579 P3) — loading / error / recovery-shell
// fallback, rendered to the HUD tokens per docs/mockup-connect-states.svg. Pure DOM
// (the connect page is vanilla, no framework); no network / mux / xterm logic lives
// here. P4 mounts this into the app-mode connect flow (the tunnel lifecycle drives the
// state transitions); it is intentionally framework-free and side-effect-free so it can
// be unit-tested and reused.
//
// Design notes it enforces (docs/design.md): the brand `--accent` (amber) is the LED for
// the live/loading state, kept SEPARATE from the status hues — red (`--status-down`) is
// reserved for active failure, amber-status (`--status-degraded`) marks the degraded
// "version skew" fallback. The "recovery shell" action is worded distinctly from RETRY so
// the fallback can't be confused with retrying the full-app tunnel (Hermes #589 note).

export type ConnectStep = { label: string; state: "done" | "active" | "pending" };

export type ConnectState =
  | { kind: "loading"; box?: string; steps?: ConnectStep[]; progress?: number }
  | { kind: "error"; title?: string; message: string }
  | { kind: "fallback"; message?: string };

export interface ConnectActions {
  /** Retry the SAME connection (full-app tunnel). Shown on the error state. */
  onRetry?: () => void;
  /** Drop to the single-terminal recovery shell — a DIFFERENT path, never a retry. */
  onRecoveryShell?: () => void;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function led(kind: "accent" | "down" | "degraded"): HTMLElement {
  return el("span", `cs-led cs-led-${kind}`);
}

function button(cls: string, label: string, onClick?: () => void): HTMLButtonElement {
  const b = el("button", cls, label);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  else b.disabled = true;
  return b;
}

const STEP_MARK: Record<ConnectStep["state"], string> = {
  done: "✓",
  active: "⟳",
  pending: "·",
};

/** Render `state` into `host`, replacing its contents. Idempotent — safe to call on every
 *  transition. `host.dataset.state` is set to the kind for styling/testing. */
export function renderConnectState(
  host: HTMLElement,
  state: ConnectState,
  actions: ConnectActions = {},
): void {
  host.textContent = "";
  host.dataset.state = state.kind;

  const header = el("div", "cs-header");
  const title = el("div", "cs-title");

  if (state.kind === "loading") {
    header.append(led("accent"), title);
    title.textContent = state.box ? `CONNECTING // ${state.box}` : "CONNECTING";
    host.append(header);

    if (state.steps?.length) {
      const list = el("ul", "cs-steps");
      for (const s of state.steps) {
        const li = el("li", `cs-step cs-step-${s.state}`);
        li.append(el("span", "cs-step-mark", STEP_MARK[s.state]), el("span", "cs-step-label", s.label));
        list.append(li);
      }
      host.append(list);
    }

    const rail = el("div", "cs-rail");
    const fill = el("div", "cs-rail-fill");
    fill.style.width = `${Math.max(0, Math.min(100, state.progress ?? 0))}%`;
    rail.append(fill);
    host.append(rail);
    host.append(el("div", "cs-note", "🔒 end-to-end encrypted · the relay is blind"));
    return;
  }

  if (state.kind === "error") {
    header.append(led("down"), title);
    title.textContent = state.title ?? "COULD NOT CONNECT";
    host.append(header, el("div", "cs-message", state.message));

    const row = el("div", "cs-actions");
    row.append(
      button("cs-btn cs-btn-primary", "RETRY", actions.onRetry),
      button("cs-btn cs-btn-ghost", "RECOVERY SHELL", actions.onRecoveryShell),
    );
    host.append(row);
    return;
  }

  // fallback — degraded (amber-status), offers ONLY the recovery shell (never a RETRY).
  header.append(led("degraded"), title);
  title.textContent = "FULL APP UNAVAILABLE";
  host.append(
    header,
    el(
      "div",
      "cs-message",
      state.message ??
        "This box can't stream the full UI yet. You can still reach it in the single-terminal recovery shell.",
    ),
  );
  const row = el("div", "cs-actions");
  row.append(button("cs-btn cs-btn-primary cs-btn-wide", "OPEN RECOVERY SHELL", actions.onRecoveryShell));
  host.append(row);
}
