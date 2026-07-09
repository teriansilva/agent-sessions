// Presentational connect-page states (#579) — loading / error. Pure DOM (the
// connect page is vanilla, no framework); no network / mux logic lives here.
// It is intentionally framework-free and side-effect-free so it can be
// unit-tested and reused.
//
// Design notes it enforces (docs/design.md): the brand `--accent` (amber) is the LED for
// the live/loading state, kept SEPARATE from the status hues — red (`--status-down`) is
// reserved for active failure.

export type ConnectStep = { label: string; state: "done" | "active" | "pending" };

export type ConnectState =
  | { kind: "loading"; box?: string; steps?: ConnectStep[]; progress?: number }
  | { kind: "error"; title?: string; message: string };

export interface ConnectActions {
  onRetry?: () => void;
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

function led(kind: "accent" | "down"): HTMLElement {
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
    row.append(button("cs-btn cs-btn-primary", "RETRY", actions.onRetry));
    host.append(row);
    return;
  }
}
