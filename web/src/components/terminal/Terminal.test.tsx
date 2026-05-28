import { fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { ThemeCtx } from "../../theme/themeStore";
import { Terminal } from "./Terminal";
import styles from "./Terminal.module.css";

vi.mock("../../lib/api", () => ({ api: { upload: vi.fn() } }));

// jsdom has no canvas/ResizeObserver/rAF — stub the bits the socket effect touches so we can
// mount the REAL Terminal (the bug this guards lives in its socket effect, not in a mock).
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    options: Record<string, unknown> = {};
    loadAddon() {}
    open() {}
    write() {}
    onData() {}
    onResize() {}
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
    fit() {}
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
}
const sockets: FakeSocket[] = [];
vi.mock("../../lib/termSocket", () => ({
  TermSocket: class {
    url: (have: number) => string;
    connect = vi.fn();
    close = vi.fn();
    send = vi.fn();
    constructor(urlFor: (have: number) => string) {
      this.url = urlFor;
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
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("requestAnimationFrame", () => 0);
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

function wrap(node: ReactNode) {
  return <ThemeCtx.Provider value={{ theme: "royal", setTheme: () => {} }}>{node}</ThemeCtx.Provider>;
}

const PLACEHOLDER = "new-11111111-1111-1111-1111-111111111111";

test("a fresh launch opens one socket whose URL carries new=1 for the placeholder id", () => {
  render(
    wrap(<Terminal engine="opencode" id={PLACEHOLDER} fresh={{ cwd: "/proj", bypass: true }} />),
  );
  expect(sockets).toHaveLength(1);
  expect(sockets[0].connect).toHaveBeenCalledTimes(1);
  const url = sockets[0].url(0);
  expect(url).toContain(`opencode:${encodeURIComponent(PLACEHOLDER)}`);
  expect(url).toContain("new=1");
  expect(url).toContain("cwd=%2Fproj");
});

// The regression for Hermes's #131 finding: SessionView drops the fresh-launch route state
// once the server reconciles the placeholder to the real id. That prop change must NOT tear
// down the live socket and reconnect without new=1 (which the server would 4404 while the id
// is still the pending placeholder), killing the terminal the converge is meant to preserve.
test("dropping `fresh` during convergence keeps the same live socket (no relaunch, no 4404)", () => {
  const { rerender } = render(
    wrap(<Terminal engine="opencode" id={PLACEHOLDER} fresh={{ cwd: "/proj", bypass: true }} />),
  );
  expect(sockets).toHaveLength(1);

  // Owner clears route state during placeholder→real converge: same key, fresh now undefined.
  rerender(wrap(<Terminal engine="opencode" id={PLACEHOLDER} fresh={undefined} />));

  // No teardown, no new socket — the socket effect is identity-only (engine:id), so the live
  // connection is preserved, and its frozen URL still carries new=1 for any future reconnect.
  expect(sockets).toHaveLength(1);
  expect(sockets[0].close).not.toHaveBeenCalled();
  expect(sockets[0].url(0)).toContain("new=1");
});

// #157: pasting an image over the terminal opens Compose and adds an attachment pill —
// it never goes to the PTY (no bracketed-paste of the server path, no terminal pollution).
test("pasting an image over the terminal routes to Compose as an attachment, not to the PTY (#157)", async () => {
  const file = new File([new Uint8Array([1, 2, 3])], "shot.png", { type: "image/png" });
  vi.mocked(api.upload).mockResolvedValue({ name: "shot.png", path: "/uploads/shot.png" });
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
