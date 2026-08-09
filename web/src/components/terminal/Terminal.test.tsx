import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { SessionsCtx } from "../../app/sessionsStore";
import { api } from "../../lib/api";
import { ThemeCtx } from "../../theme/themeStore";
import type { Session } from "../../types/api";
import { Terminal } from "./Terminal";
import styles from "./Terminal.module.css";

vi.mock("../../lib/api", () => ({
  // getDraft/saveDraft (#477): Compose loads its draft on mount now. Resolve to an empty
  // draft so the load is a no-op and these Terminal tests stay about the socket/terminal.
  api: {
    upload: vi.fn(),
    getDraft: vi
      .fn()
      .mockResolvedValue({
        id: "",
        text: "",
        attachments: [],
        updated_at: null,
      }),
    saveDraft: vi.fn().mockResolvedValue({ id: "", has_draft: false }),
  },
}));

// jsdom has no canvas/ResizeObserver/rAF — stub the bits the socket effect touches so we can
// mount the REAL Terminal (the bug this guards lives in its socket effect, not in a mock).
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
// Records every addon loaded into the Terminal so tests can assert WebLinksAddon's presence
// (and that its click handler was wired correctly). Reset in `beforeEach`.
const loadedAddons: unknown[] = [];
// Per-test handle to the latest mocked xterm so #187 can poke its scroll bookkeeping.
type FakeXterm = {
  buffer: { active: { baseY: number; viewportY: number } };
  scrollToBottom: ReturnType<typeof vi.fn>;
  fireScroll: () => void;
  paste: ReturnType<typeof vi.fn>;
};
const xterms: FakeXterm[] = [];
// Drives a CHANGING grid for the connect-when-quiet test: each fit() applies the next entry to the
// live xterm and advances (holding the last). Empty (the default) → fit is a no-op and the mocked
// xterm keeps its constant 80×24, so every other test is unaffected.
let gridScript: Array<{ cols: number; rows: number }> = [];
let gridIdx = 0;
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    options: Record<string, unknown> = {};
    buffer = { active: { baseY: 0, viewportY: 0 } };
    private scrollCb: (() => void) | undefined;
    public scrollToBottom = vi.fn(() => {
      this.buffer.active.viewportY = this.buffer.active.baseY;
      this.scrollCb?.();
    });
    public paste = vi.fn();
    constructor() {
      const self = this as unknown as FakeXterm;
      self.fireScroll = () => this.scrollCb?.();
      xterms.push(self);
    }
    loadAddon(addon: unknown) {
      loadedAddons.push(addon);
    }
    open() {}
    write() {}
    onData() {}
    onResize() {}
    attachCustomKeyEventHandler() {}
    onScroll(cb: () => void) {
      this.scrollCb = cb;
    }
    onWriteParsed() {
      return { dispose() {} };
    }
    dispose() {}
    getSelection() {
      return "";
    }
    selectAll() {}
    clearSelection() {}
  },
}));
vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    // Default: no-op (constant 80×24). When a test sets gridScript, apply the next size to the live
    // xterm and advance — so connectWhenStable sees a grid that settles over several frames.
    fit() {
      if (!gridScript.length) return;
      const term = xterms[xterms.length - 1] as unknown as
        { cols: number; rows: number } | undefined;
      if (!term) return;
      term.cols = gridScript[gridIdx].cols;
      term.rows = gridScript[gridIdx].rows;
      if (gridIdx < gridScript.length - 1) gridIdx++;
    }
  },
}));
// Capture the click handler passed to WebLinksAddon so the test can fire it directly and
// confirm the window.open invocation shape (noopener,noreferrer).
vi.mock("@xterm/addon-web-links", () => ({
  WebLinksAddon: class {
    public handler: (e: unknown, uri: string) => void;
    constructor(handler: (e: unknown, uri: string) => void) {
      this.handler = handler;
    }
  },
}));

// Capture every TermSocket the component constructs: its url factory + connect/close calls.
// termUrl is left REAL so we can assert new=1 survives. The point of the regression is that a
// converge (drop of `fresh`) must NOT spawn a second socket / a new=1-less reconnect.
interface FakeSocket {
  url: (have: number) => string;
  connect: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  // #184 hook: tests can fire the role frame as if the server sent one.
  emitRole?: (role: "owner" | "secondary") => void;
  // Grid size measured at the moment connect() fired — lets the connect-when-quiet test assert the
  // attach happened at the SETTLED size, not an in-flight one. Captured from the live xterm directly
  // (NOT via url(), which would consume the one-shot force flag the #184 takeover test depends on).
  connectCols?: number;
  connectRows?: number;
}
const sockets: FakeSocket[] = [];
vi.mock("../../lib/termSocket", () => ({
  TermSocket: class {
    url: (have: number) => string;
    connect: ReturnType<typeof vi.fn>;
    close = vi.fn();
    send = vi.fn();
    emitRole: (role: "owner" | "secondary") => void;
    connectCols?: number;
    connectRows?: number;
    constructor(
      urlFor: (have: number) => string,
      handlers: { onRole?: (role: "owner" | "secondary") => void },
    ) {
      this.url = urlFor;
      this.connect = vi.fn(() => {
        const term = xterms[xterms.length - 1] as unknown as
          { cols: number; rows: number } | undefined;
        this.connectCols = term?.cols;
        this.connectRows = term?.rows;
      });
      this.emitRole = (role) => handlers.onRole?.(role);
      sockets.push(this as unknown as FakeSocket);
    }
  },
}));

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

beforeEach(() => {
  sockets.length = 0;
  loadedAddons.length = 0;
  xterms.length = 0;
  gridScript = [];
  gridIdx = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  // Invoke the callback synchronously: the connect-when-stable settle loop (#299) re-measures
  // across frames until the grid holds steady, and the mocked xterm reports a constant 80×24, so
  // it settles + connects within a couple of synchronous ticks.
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 0;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

function wrap(node: ReactNode) {
  // Terminal uses useNavigate (gate Cancel → new session, #293), so it needs a Router context.
  return (
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: () => {} }}>
        {node}
      </ThemeCtx.Provider>
    </MemoryRouter>
  );
}

const PLACEHOLDER = "new-11111111-1111-1111-1111-111111111111";

test("a fresh launch opens one socket whose URL carries new=1 for the placeholder id", () => {
  render(
    wrap(
      <Terminal
        engine="opencode"
        id={PLACEHOLDER}
        fresh={{ cwd: "/proj", bypass: true }}
      />,
    ),
  );
  expect(sockets).toHaveLength(1);
  expect(sockets[0].connect).toHaveBeenCalledTimes(1);
  const url = sockets[0].url(0);
  expect(url).toContain(`opencode:${encodeURIComponent(PLACEHOLDER)}`);
  expect(url).toContain("new=1");
  expect(url).toContain("cwd=%2Fproj");
});

test("does not connect until the grid settles — guards the fragments-on-switch race", () => {
  // The bug: on a fast in-app session switch the panel height isn't final on mount (observed
  // rows 61→66). Connecting at the un-settled size, then correcting it, SIGWINCHes the agent
  // into a clear+repaint that WIPES the just-delivered transcript scroll-up. Fix: connect only
  // once the measured grid holds steady across frames. With rAF suppressed the settle loop can't
  // advance, so connect must NOT have fired — proving the attach is deferred, not synchronous.
  vi.stubGlobal("requestAnimationFrame", () => 0); // never settles → never connects
  render(wrap(<Terminal engine="claude" id="abc" />));
  expect(sockets).toHaveLength(1);
  expect(sockets[0].connect).not.toHaveBeenCalled();
});

test("attaches at the SETTLED grid, not an in-flight one — fixes 'switch almost always needs F5'", () => {
  // The real failure: on an in-app session switch the panel keeps resizing for several frames after
  // mount (mobile drawer-close / address-bar settle — the observed rows 61→66). The OLD loop trusted
  // the FIRST two equal frames, so it attached at the in-flight 61, then the settle to 66 SIGWINCHed
  // the agent into a clear+repaint that WIPED the scroll-up → only F5 (a fresh, pre-settled load)
  // recovered. The grid below holds 61 for two frames (enough to fool the old "two agree" rule), then
  // settles to 66. The fix waits for a QUIET window, so connect must land at 66 — the final size.
  gridScript = [
    { cols: 80, rows: 61 },
    { cols: 80, rows: 61 },
    { cols: 80, rows: 66 },
  ];
  render(wrap(<Terminal engine="claude" id="abc" />));
  expect(sockets).toHaveLength(1);
  expect(sockets[0].connect).toHaveBeenCalledTimes(1);
  // Attached at the settled grid (66), NOT the transient 61 the old code would have used.
  expect(sockets[0].connectRows).toBe(66);
  expect(sockets[0].connectCols).toBe(80);
});

// The regression for Hermes's #131 finding: SessionView drops the fresh-launch route state
// once the server reconciles the placeholder to the real id. That prop change must NOT tear
// down the live socket and reconnect without new=1 (which the server would 4404 while the id
// is still the pending placeholder), killing the terminal the converge is meant to preserve.
test("dropping `fresh` during convergence keeps the same live socket (no relaunch, no 4404)", () => {
  const { rerender } = render(
    wrap(
      <Terminal
        engine="opencode"
        id={PLACEHOLDER}
        fresh={{ cwd: "/proj", bypass: true }}
      />,
    ),
  );
  expect(sockets).toHaveLength(1);

  // Owner clears route state during placeholder→real converge: same key, fresh now undefined.
  rerender(
    wrap(<Terminal engine="opencode" id={PLACEHOLDER} fresh={undefined} />),
  );

  // No teardown, no new socket — the socket effect is identity-only (engine:id), so the live
  // connection is preserved, and its frozen URL still carries new=1 for any future reconnect.
  expect(sockets).toHaveLength(1);
  expect(sockets[0].close).not.toHaveBeenCalled();
  expect(sockets[0].url(0)).toContain("new=1");
});

// #157: pasting an image over the terminal opens Compose and adds an attachment pill —
// it never goes to the PTY (no bracketed-paste of the server path, no terminal pollution).
test("pasting an image over the terminal routes to Compose as an attachment, not to the PTY (#157)", async () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", {
    type: "image/png",
  });
  vi.mocked(api.upload).mockResolvedValue({
    name: "shot.png",
    path: "/uploads/shot.png",
  });
  const { container } = render(wrap(<Terminal engine="claude" id="abc123" />));
  const host = container.getElementsByClassName(styles.term)[0];
  expect(host).toBeTruthy();
  fireEvent.paste(host, {
    clipboardData: {
      items: [{ kind: "file", type: "image/png", getAsFile: () => file }],
      files: [file],
    },
  });
  // Compose's uploadFiles is invoked → api.upload runs with the pasted file.
  await vi.waitFor(() => expect(api.upload).toHaveBeenCalledWith(file));
  // The pill (with the filename) appears in the DOM → the user sees the attachment landed.
  await screen.findByText("shot.png");
  // CRITICAL: nothing was bracketed-pasted into the PTY for the image.
  const sentToPty = sockets[0].send.mock.calls
    .map((c) => c[0])
    .filter((m) => m.t === "i")
    .map((m) => m.d);
  expect(sentToPty.join("|")).not.toContain("/uploads/shot.png");
});

// #158: URLs in agent output are clickable via @xterm/addon-web-links. The addon is loaded
// on every Terminal mount with a handler that opens in a new tab WITHOUT window.opener +
// WITHOUT the Referer header.
test("loads WebLinksAddon and opens links in a new tab with noopener,noreferrer (#158)", () => {
  const open = vi.fn();
  vi.stubGlobal("open", open);
  render(wrap(<Terminal engine="claude" id="abc123" />));

  // The Terminal effect loaded the addons in order: FitAddon, then WebLinksAddon.
  // Find the WebLinksAddon by its captured `handler` property.
  const wl = loadedAddons.find(
    (a): a is { handler: (e: unknown, uri: string) => void } =>
      typeof (a as { handler?: unknown }).handler === "function",
  );
  expect(wl).toBeDefined();

  // Fire the handler as the addon would on a real click.
  wl!.handler({}, "https://example.com/foo");
  expect(open).toHaveBeenCalledWith(
    "https://example.com/foo",
    "_blank",
    "noopener,noreferrer",
  );
});

// #187: floating scroll-to-bottom button. Mounts on EVERY pointer type whenever the
// viewport is off the live tail; tapping it calls term.scrollToBottom() and the
// button auto-hides.
function setCoarsePointer(coarse: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: q.includes("pointer: coarse") ? coarse : false,
    addEventListener() {},
    removeEventListener() {},
  }));
}

test("#187 FAB stays hidden when the viewport sits on the live tail", () => {
  setCoarsePointer(true);
  render(wrap(<Terminal engine="claude" id="abc" />));
  expect(
    screen.queryByRole("button", { name: /scroll to bottom/i }),
  ).toBeNull();
});

test("#187 FAB appears when the user scrolls off the tail, dismisses on tap", () => {
  setCoarsePointer(true);
  render(wrap(<Terminal engine="claude" id="abc" />));
  const term = xterms[0];

  // Simulate the user scrolling well above the tail.
  act(() => {
    term.buffer.active.baseY = 100;
    term.buffer.active.viewportY = 40;
    term.fireScroll();
  });

  const fab = screen.getByRole("button", { name: /scroll to bottom/i });
  expect(fab).toBeInTheDocument();

  // Tap → scrollToBottom called + position lands on the tail → button hides.
  fireEvent.click(fab);
  expect(term.scrollToBottom).toHaveBeenCalledTimes(1);
  expect(
    screen.queryByRole("button", { name: /scroll to bottom/i }),
  ).toBeNull();
});

test("#187 FAB shows on a fine-pointer (desktop) device too", () => {
  // Regression: the button used to be gated behind coarse pointers, leaving desktop users
  // who scrolled up into history with no one-click way back to the tail. It now shows on
  // every pointer type whenever the viewport is off the live tail.
  setCoarsePointer(false);
  render(wrap(<Terminal engine="claude" id="abc" />));
  const term = xterms[0];

  // On the tail: hidden.
  expect(
    screen.queryByRole("button", { name: /scroll to bottom/i }),
  ).toBeNull();

  // Scrolled off the tail: visible, and a click jumps back to the bottom.
  act(() => {
    term.buffer.active.baseY = 100;
    term.buffer.active.viewportY = 0;
    term.fireScroll();
  });
  const fab = screen.getByRole("button", { name: /scroll to bottom/i });
  expect(fab).toBeInTheDocument();
  fireEvent.click(fab);
  expect(term.scrollToBottom).toHaveBeenCalledTimes(1);
  expect(
    screen.queryByRole("button", { name: /scroll to bottom/i }),
  ).toBeNull();
});

// #184: per-tab ownership protocol — the URL must carry fp + tab so the server
// SessionRegistry can claim correctly; the secondary banner renders when the
// server sends {t:"role","role":"secondary"} and the Take-over button forces a
// reconnect with ?force=1.
test("#184 termWsUrl includes fp + tab params on every connect", () => {
  render(wrap(<Terminal engine="claude" id="abc" />));
  const url = sockets[0].url(0);
  expect(url).toMatch(/[?&]fp=[0-9a-f]{32}/);
  expect(url).toMatch(/[?&]tab=[0-9a-f]{16}/);
  // No force=1 on a first attach — only on takeover.
  expect(url).not.toMatch(/[?&]force=1/);
});

test("#184 secondary role surfaces the read-only banner + Take-over button", () => {
  render(wrap(<Terminal engine="claude" id="abc" />));
  // Before the server speaks, no banner.
  expect(screen.queryByRole("button", { name: /take over/i })).toBeNull();

  act(() => {
    sockets[0].emitRole!("secondary");
  });
  // Banner explains the state + Take-over button is wired.
  expect(screen.getByText(/read-only mode/i)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /take over/i }),
  ).toBeInTheDocument();
});

test("#184 Take-over reconnects with ?force=1 exactly once", () => {
  render(wrap(<Terminal engine="claude" id="abc" />));
  act(() => sockets[0].emitRole!("secondary"));
  expect(sockets).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: /take over/i }));

  // Bumping the takeover epoch tears down the old socket and opens a fresh one.
  expect(sockets).toHaveLength(2);
  // The first call from the fresh socket carries force=1 — the next call (a
  // transient reconnect after a drop) MUST NOT keep demanding takeover.
  const firstUrl = sockets[1].url(0);
  const secondUrl = sockets[1].url(0);
  expect(firstUrl).toMatch(/[?&]force=1/);
  expect(secondUrl).not.toMatch(/[?&]force=1/);
});

test("#184 owner role hides the banner", () => {
  render(wrap(<Terminal engine="claude" id="abc" />));
  act(() => sockets[0].emitRole!("secondary"));
  expect(
    screen.getByRole("button", { name: /take over/i }),
  ).toBeInTheDocument();
  act(() => sockets[0].emitRole!("owner"));
  expect(screen.queryByRole("button", { name: /take over/i })).toBeNull();
});

// #181: text pasted anywhere over the terminal pane is forwarded to xterm via
// term.paste(text). Without this the paste depended on the hidden helper textarea
// receiving the event, which failed unreliably and forced the user to use
// right-click → Paste from the context menu.
test("#181 text paste over the terminal forwards to term.paste + prevents default", () => {
  const { container } = render(wrap(<Terminal engine="claude" id="abc" />));
  const host = container.getElementsByClassName(styles.term)[0] as HTMLElement;
  expect(host).toBeTruthy();
  const preventDefault = vi.fn();
  const stopPropagation = vi.fn();
  fireEvent.paste(host, {
    clipboardData: {
      items: [{ kind: "string", type: "text/plain" }],
      files: [] as File[],
      getData: (mime: string) => (mime === "text/plain" ? "hello agent" : ""),
    },
    preventDefault,
    stopPropagation,
  });
  expect(xterms[0].paste).toHaveBeenCalledWith("hello agent");
});

function headerRow(over: Record<string, unknown> = {}): Session[] {
  return [
    {
      id: "claude:abc",
      engine: "claude",
      uuid: "abc",
      short_uuid: "abc",
      cwd: "/home/u/proj/api",
      project: {
        kind: "folder",
        id: "/home/u/proj/api",
        name: "/home/u/proj/api",
      },
      last_mtime: Math.floor(Date.now() / 1000) - 172_800,
      first_user_message: "a",
      title: "",
      sticky: false,
      archived: false,
      ...over,
    },
  ] as unknown as Session[];
}

function renderTerminal(sessions: Session[]) {
  return render(
    <MemoryRouter>
      <ThemeCtx.Provider value={{ theme: "dark", setTheme: () => {} }}>
        <SessionsCtx.Provider value={{ sessions, setSessions: () => {} }}>
          <Terminal engine="claude" id="abc" />
        </SessionsCtx.Provider>
      </ThemeCtx.Provider>
    </MemoryRouter>,
  );
}

// #744: the header is a meta run — LED, engine box, project, update time. The engine word and
// the truncated UUID that used to lead the bar are gone; so is the "STATUS //" label, whose job
// the LED beside it was already doing.
test("the panel header shows the engine box, project and update time (#744)", () => {
  renderTerminal(headerRow());
  expect(screen.getByTitle("claude")).toHaveTextContent("cc");
  expect(screen.getByText("~/proj/api")).toBeInTheDocument();
  expect(screen.getByText(/2 days ago/)).toBeInTheDocument();
  // The retired chrome.
  expect(screen.queryByText(/CLAUDE \/\//)).not.toBeInTheDocument();
  expect(screen.queryByText(/STATUS/)).not.toBeInTheDocument();
  expect(screen.queryByText("abc…")).not.toBeInTheDocument();
});

// The LED replaced a text readout, so its meaning must not become colour-only.
test("the header LED keeps an accessible name for the link state (#744)", () => {
  renderTerminal(headerRow());
  expect(screen.getByRole("img", { name: /^status: /i })).toBeInTheDocument();
});

// #284 (re-homed by #744): the resolved display title no longer renders in the header, but the
// same rule still holds where it DOES render — a meaningless one-char first message must never
// leak; the title drops to the short id instead.
test("the display title falls back to the short id — a one-char first message never leaks (#284)", async () => {
  renderTerminal(headerRow());
  await userEvent.click(
    screen.getByRole("button", { name: /open session brief/i }),
  );
  expect(screen.getByRole("dialog", { name: "abc…" })).toBeInTheDocument();
});

// An adopted project shows its entity name, not the launch folder (sidebar parity).
test("an adopted project shows its entity name in the header (#744)", () => {
  renderTerminal(
    headerRow({ project: { kind: "project", id: "p-1", name: "BattleLab" } }),
  );
  expect(screen.getByText("BattleLab")).toBeInTheDocument();
});
