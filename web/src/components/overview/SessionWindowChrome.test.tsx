import { render, screen } from "@testing-library/react";
import { act, useEffect } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { SessionWindow } from "./SessionWindow";
import type { HeadAction } from "../terminal/HeadActions";
import type { MenuAnchor, RowMenuEntry } from "../sidebar/RowMenu";

// The window mounts the SAME <Terminal> the /s/:engine/:id route uses. Mocked here so the
// #1109 CHROME wiring — head suppression, the chips slot, the fold's overflow ref, the file
// drawer handoff — can be asserted without xterm; the pane's own behaviour is the e2e's job.
type Captured = Record<string, unknown>;
const captured: Captured[] = [];

/** What a mounted HeadActions would publish through `headOverflowRef` — the chrome reads this
 *  ref at menu-open time, so the test seeds it the same way the real fold does. */
const OVERFLOW: HeadAction[] = [
  {
    id: "repaint",
    label: "Repaint",
    aria: "Repaint screen",
    title: "Repaint the screen",
    icon: null,
    run: () => {},
  },
  {
    id: "recap",
    label: "Recap",
    aria: "Open session brief",
    title: "Session brief",
    icon: null,
    run: () => {},
  },
  {
    id: "handoff",
    label: "Hand off",
    aria: "Hand off session to another engine",
    title: "Hand off",
    icon: null,
    run: () => {},
  },
  {
    id: "text-smaller",
    label: "Smaller text",
    aria: "Smaller terminal text",
    title: "Smaller text",
    icon: null,
    run: () => {},
  },
];

vi.mock("../terminal/Terminal", () => ({
  Terminal: (props: Captured) => {
    captured.push(props);
    // Publish the fold exactly once, as HeadActions would after its first commit.
    useEffect(() => {
      const ref = props.headOverflowRef as { current: HeadAction[] } | undefined;
      if (ref) ref.current = OVERFLOW;
    });
    return <div data-testid="term" />;
  },
}));

const panelState = vi.hoisted(() => ({ mounts: 0 }));
vi.mock("../files/FilePanel", () => ({
  FilePanel: (props: Captured) => {
    useEffect(() => {
      panelState.mounts += 1;
    }, []);
    return (
      <div data-testid="file-panel" data-props={JSON.stringify(props)} />
    );
  },
}));

const BASE = {
  wkey: "claude:abc",
  engine: "claude",
  id: "abc",
  actionKey: "claude:abc",
  title: "A session",
  rect: { x: 0, y: 0, w: 720, h: 480 },
  bounds: { w: 1400, h: 900 },
  focused: true,
  role: "owner" as const,
  onFocus: () => {},
  onClose: () => {},
  onFullScreen: () => {},
  onRect: () => {},
  onRole: () => {},
  onReconcile: () => {},
};

function renderWindow(props: Partial<typeof BASE> & { onMenu?: (...a: unknown[]) => void } = {}) {
  return render(
    <MemoryRouter initialEntries={["/overview"]}>
      <Routes>
        <Route path="/overview" element={<SessionWindow {...BASE} {...props} />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  captured.length = 0;
});

test("the chrome suppresses the pane's own bar and hands it a chips slot (#1109)", () => {
  renderWindow();
  const last = captured[captured.length - 1];
  expect(last.suppressHead).toBe(true);
  // The slot element exists in the chrome; the pane receives it to portal its chips into.
  const slot = document.querySelector("[data-window-actions-slot]");
  expect(slot).not.toBeNull();
  expect(last.headActionsSlot).toBe(slot);
  expect(last.headOverflowRef).toBeDefined();
  // The fold's reserve is the measured facts run + the chrome's fixed remainder.
  expect(last.headReservePx).toBeGreaterThan(0);
  // The pane publishes its socket status; the chrome renders the LED from it.
  expect(typeof last.onTermStatus).toBe("function");
  // The facts run renders in the chrome itself (the shared HeadFacts), and the pane's own bar
  // is gone — asserted on the DOM in the browser spec.
  expect(document.querySelector("[data-window-facts]")).not.toBeNull();
});

test("the ⋯ opens the canvas's merged menu with the fold's overflow, deduped (#1109)", async () => {
  const onMenu = vi.fn();
  const user = userEvent.setup();
  renderWindow({ onMenu });
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  expect(onMenu).toHaveBeenCalledTimes(1);
  const [wkey, anchor, opener, paneItems] = onMenu.mock.calls[0] as [
    string,
    MenuAnchor,
    HTMLElement | null,
    RowMenuEntry[],
  ];
  expect(wkey).toBe("claude:abc");
  expect(anchor).toHaveProperty("element");
  expect(opener).not.toBeNull();
  // The pane group carries the fold, as menu entries — minus the actions the session group
  // already covers (Recap → Session brief, Hand off → Hand off…). Repaint and the text-size
  // pair remain.
  const keys = paneItems
    .filter((e): e is Extract<RowMenuEntry, { key: string }> => typeof e === "object" && "key" in e)
    .map((e) => e.key);
  expect(keys).toEqual(["repaint", "text-smaller"]);
});

test("off the map the ⋯ opens a local PANE-ONLY menu instead (#1109)", async () => {
  const onMenu = vi.fn();
  const user = userEvent.setup();
  renderWindow({
    onMenu,
    offMapReason: "This session isn't on the map right now",
  });
  const btn = screen.getByRole("button", { name: "Session actions" });
  // The state explains itself on the control, not by hiding it…
  expect(btn).toHaveAttribute("title", expect.stringContaining("isn't on the map"));
  // …and the control still WORKS: the pane actions never needed the map's row.
  await user.click(btn);
  expect(onMenu).not.toHaveBeenCalled();
  const menu = screen.getByRole("menu", { name: "Pane actions" });
  expect(menu).toBeInTheDocument();
  // No dedupe here — the session group is absent, so everything the chips folded is here.
  const items = menu.querySelectorAll("[role='menuitem']");
  expect(items.length).toBe(OVERFLOW.length);
  // No "Session" group label — the row-dependent actions are the ones absent.
  expect(menu.querySelector("[data-menu-group='Session']")).toBeNull();
});

test("the Files toggle opens the drawer in the window body, transient and contained (#1109)", async () => {
  renderWindow({ fresh: { cwd: "/home/u/proj", bypass: true } });
  const last = captured[captured.length - 1];
  expect(typeof last.onToggleFiles).toBe("function");
  // A fresh launch carries its cwd in the record, so the trigger is LIVE immediately.
  expect(last.filesDisabledReason).toBeUndefined();
  await act(async () => {
    (last.onToggleFiles as (t?: HTMLElement | null) => void)();
  });
  const panel = screen.getByTestId("file-panel");
  const props = JSON.parse(panel.getAttribute("data-props") ?? "{}") as Record<string, unknown>;
  expect(props.sessionKey).toBe("claude:abc");
  expect(props.cwd).toBe("/home/u/proj");
  // The window's drawer state is its own: it does not write the pane's remembered-open flag.
  expect(props.persistOpen).toBe(false);
  // bodyW is 0 in jsdom (no layout) → sheet mode → contained.
  expect(props.contained).toBe(true);
});

test("an id reconcile never remounts the drawer: mount identity is the WINDOW (#1109 P1)", async () => {
  // The fresh-launch placeholder reconciles to the engine's real id MID-LIFE — an automatic
  // transition. The panel's MOUNT identity must be the window (stable), its SERVER identity
  // the action id (following). A key that tracked the action id would unmount the panel on
  // the transition and take a dirty editor with it.
  panelState.mounts = 0;
  const view = renderWindow({
    actionKey: "claude:new-launch",
    fresh: { cwd: "/home/u/proj", bypass: true },
  });
  const last = captured[captured.length - 1];
  await act(async () => {
    (last.onToggleFiles as (t?: HTMLElement | null) => void)();
  });
  const before = JSON.parse(
    screen.getByTestId("file-panel").getAttribute("data-props") ?? "{}",
  ) as Record<string, unknown>;
  expect(before.sessionKey).toBe("claude:new-launch");
  expect(panelState.mounts).toBe(1);

  // The {"t":"id"} frame lands: the workspace re-renders the window with the converged id.
  await act(async () => {
    view.rerender(
      <MemoryRouter initialEntries={["/overview"]}>
        <Routes>
          <Route
            path="/overview"
            element={
              <SessionWindow
                {...BASE}
                actionKey="claude:real-1111"
                fresh={{ cwd: "/home/u/proj", bypass: true }}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );
  });

  // Same mounted panel — ONE mount for the whole life — and its server identity followed.
  expect(panelState.mounts).toBe(1);
  const after = JSON.parse(
    screen.getByTestId("file-panel").getAttribute("data-props") ?? "{}",
  ) as Record<string, unknown>;
  expect(after.sessionKey).toBe("claude:real-1111");
  expect(after.cwd).toBe("/home/u/proj");
});
