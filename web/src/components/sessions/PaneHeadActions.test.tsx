import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import { SESSION_ROW_CHANGED_EVENT, type SessionRowChange } from "../../lib/sessionRowEvents";
import type { Session } from "../../types/api";
import type { HeadAction } from "../terminal/HeadActions";
import { PaneHeadActions } from "./PaneHeadActions";
import { shareHeadAction } from "./shareAction";

/** The session pane's own menu is the merged menu a map window has (#2173 P2): the Session group
 *  (`useSessionMenu`), then the Pane group (the head actions that are not chips), with the session
 *  twins of the pane's actions named once. The fixture roster: `claude` is a terminal client,
 *  `claude-api` an API one. jsdom measures nothing, so the fold is driven by `collapsed`: the
 *  compact header folds EVERY action, the wide one none. */

vi.mock("../shares/SharePubliclyModal", () => ({
  SharePubliclyModal: () => <div role="dialog" aria-label="Share publicly" />,
}));
vi.mock("../federation/SharePeerModal", () => ({
  SharePeerModal: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="Share with a peer">
      <button type="button" onClick={onClose}>
        close peer
      </button>
    </div>
  ),
}));
vi.mock("../sidebar/MoveToProjectModal", () => ({
  MoveToProjectModal: ({ onCancel }: { onCancel: () => void }) => (
    <div role="dialog" aria-label="Move to project">
      <button type="button" onClick={onCancel}>
        cancel move
      </button>
    </div>
  ),
}));

function sess(engine: string, over: Partial<Session> = {}): Session {
  const id = `${engine}:11111111-1111-4111-8111-111111111111`;
  return {
    id,
    engine,
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/proj",
    project: { kind: "folder" as const, id: "/home/m/proj", name: "/home/m/proj" },
    last_mtime: 1,
    first_user_message: "",
    title: "A session",
    sticky: false,
    archived: false,
    mission: null,
    ...over,
  };
}

const ran = vi.fn();
const act1 = (id: string, label: string, aria = label): HeadAction => ({
  id,
  label,
  aria,
  title: label,
  icon: null,
  run: (trigger) => ran(id, trigger),
});
/** The terminal pane's head, in its order (Terminal.tsx): Files, Recap, Hand off, Share, Repaint. */
const terminalHead = (): HeadAction[] => [
  act1("files", "Files", "Browse session files"),
  act1("recap", "Recap", "Open session brief"),
  act1("handoff", "Hand off", "Hand off session to another engine"),
  shareHeadAction({ subject: "terminal", onPeer: (t) => ran("share-peer", t) }),
  act1("restart", "Repaint", "Repaint terminal"),
];
/** The API pane's head (StructuredPane.tsx): the same session actions, plus its mission action. */
const apiHead = (): HeadAction[] => [
  act1("files", "Files", "Browse session files"),
  act1("recap", "Recap", "Open session brief"),
  act1("handoff", "Hand off", "Hand off session to another engine"),
  shareHeadAction({
    subject: "api",
    onPublic: (t) => ran("share-public", t),
    onPeer: (t) => ran("share-peer", t),
  }),
  act1("mission", "Adopt to mission", "Adopt session to a mission"),
];

function mount(row: Session | undefined, actions: HeadAction[], collapsed: boolean) {
  return render(
    <MemoryRouter initialEntries={["/s/x/y"]}>
      <Routes>
        <Route
          path="/s/:engine/:id"
          element={
            <PaneHeadActions
              row={row}
              actions={actions}
              collapsed={collapsed}
              className="head"
              btnClassName="btn"
              labelClassName="lbl"
            />
          }
        />
        <Route path="*" element={<div data-testid="left-the-pane" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const trigger = () => document.querySelector<HTMLElement>("[data-head-more]");
async function openMenu() {
  await userEvent.click(trigger()!);
  return screen.getByRole("menu");
}
/** The menu as a flat reading: `#Group` for a label, the accessible name for an item. */
function reading(menu: HTMLElement): string[] {
  return Array.from(menu.querySelectorAll<HTMLElement>("[data-menu-group], [role='menuitem']")).map(
    (el) => (el.dataset.menuGroup ? `#${el.dataset.menuGroup}` : (el.getAttribute("aria-label") ?? el.textContent ?? "")),
  );
}
const count = (list: string[], name: string) => list.filter((x) => x === name).length;

let announced: SessionRowChange[] = [];
const onAnnounce = (e: Event) => announced.push((e as CustomEvent<SessionRowChange>).detail);
beforeEach(() => {
  announced = [];
  ran.mockClear();
  window.addEventListener(SESSION_ROW_CHANGED_EVENT, onAnnounce);
});
afterEach(() => {
  window.removeEventListener(SESSION_ROW_CHANGED_EVENT, onAnnounce);
  vi.restoreAllMocks();
});

test("a terminal pane, compact: Session group then Pane group, every action named once", async () => {
  mount(sess("claude"), terminalHead(), true);
  // The compact header's ONE trigger keeps its name and test id.
  expect(trigger()).toHaveAttribute("data-testid", "head-actions-menu");
  const list = reading(await openMenu());
  expect(count(list, "#Session")).toBe(1);
  expect(count(list, "#Pane")).toBe(1);
  expect(count(list, "#Share")).toBe(1);
  expect(list.indexOf("#Session")).toBeLessThan(list.indexOf("#Pane"));
  // The pane's actions are in the Pane group, their session twins are not repeated above it.
  for (const name of [
    "Open session brief",
    "Hand off session to another engine",
    "Share this session publicly",
    "Share this terminal's screen with a paired instance",
  ]) {
    expect(count(list, name)).toBe(1);
    expect(list.indexOf(name)).toBeGreaterThan(list.indexOf("#Pane"));
  }
  // Share sits under Pane here, because the pane folded it.
  expect(list.indexOf("#Share")).toBeGreaterThan(list.indexOf("#Pane"));
  // The session-only run is all there, above the Pane group.
  for (const name of [
    "Favorite session",
    "Rename session",
    "Set session tag",
    "Move session to a project",
    "Archive session",
  ]) {
    expect(count(list, name)).toBe(1);
    expect(list.indexOf(name)).toBeLessThan(list.indexOf("#Pane"));
  }
  // A terminal session is not adoptable (#1902), from either group.
  expect(list).not.toContain("Adopt session to a mission");
  expect(list.slice(list.indexOf("#Pane") + 1)).toEqual([
    "Browse session files",
    "Open session brief",
    "Hand off session to another engine",
    "#Share",
    "Share this session publicly",
    "Share this terminal's screen with a paired instance",
    "Repaint terminal",
  ]);
});

test("an API pane, compact: the same two groups, the mission action once, under Pane", async () => {
  mount(sess("claude-api"), apiHead(), true);
  const list = reading(await openMenu());
  expect(count(list, "#Session")).toBe(1);
  expect(count(list, "#Pane")).toBe(1);
  expect(count(list, "#Share")).toBe(1);
  for (const name of [
    "Open session brief",
    "Hand off session to another engine",
    "Share this session publicly",
    "Share this session with a paired instance",
    "Adopt session to a mission",
  ]) {
    expect(count(list, name)).toBe(1);
    expect(list.indexOf(name)).toBeGreaterThan(list.indexOf("#Pane"));
  }
  expect(list).toContain("Archive session");
});

test("wide, every chip on the bar: the … is still there and carries the Session group alone", async () => {
  mount(sess("claude-api"), apiHead(), false);
  expect(document.querySelectorAll("[data-head-action]")).toHaveLength(5);
  expect(trigger()).toHaveAccessibleName("More session actions");
  const list = reading(await openMenu());
  expect(count(list, "#Session")).toBe(1);
  expect(list).not.toContain("#Pane");
  // The pane offers these as chips, so the menu does not name them a second time.
  expect(list).not.toContain("#Share");
  expect(list).not.toContain("Open session brief");
  expect(list).not.toContain("Hand off session to another engine");
  expect(list).not.toContain("Adopt session to a mission");
  expect(list).toContain("Rename session");
  expect(list).toContain("Archive session");
});

test("no row yet: the pane-only menu, and no trigger at all when nothing folded", async () => {
  const wide = mount(undefined, terminalHead(), false);
  expect(trigger()).toBeNull();
  wide.unmount();
  mount(undefined, terminalHead(), true);
  const menu = await openMenu();
  const list = reading(menu);
  expect(list).not.toContain("#Session");
  expect(list).not.toContain("#Pane");
  expect(list).not.toContain("Archive session");
  expect(count(list, "#Share")).toBe(1);
  expect(list).toContain("Repaint terminal");
});

test("a Pane item runs with the trigger, which is where focus returns", async () => {
  mount(sess("claude"), terminalHead(), true);
  const menu = await openMenu();
  await userEvent.click(within(menu).getByRole("menuitem", { name: "Open session brief" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(ran).toHaveBeenCalledWith("recap", trigger());
  expect(document.activeElement).toBe(trigger());
});

test("a dialog opened from the Session group outlives the menu and hands focus back to the …", async () => {
  mount(sess("claude"), terminalHead(), false);
  const menu = await openMenu();
  await userEvent.click(within(menu).getByRole("menuitem", { name: "Move session to a project" }));
  // The menu is gone; the host is not, or its dialog would have gone with it.
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("dialog", { name: "Move to project" })).toBeInTheDocument();
  expect(trigger()).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(screen.getByRole("button", { name: "cancel move" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  // The trigger never unmounted, so it can be opened again.
  expect(reading(await openMenu())).toContain("Rename session");
});

test("Favorite calls the route and announces the server's value for the list and the store", async () => {
  const s = sess("claude");
  vi.spyOn(api, "favorite").mockResolvedValue({ id: s.id, sticky: true });
  mount(s, terminalHead(), false);
  await userEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Favorite session" }));
  await waitFor(() => expect(announced).toEqual([{ key: s.id, patch: { sticky: true } }]));
  expect(api.favorite).toHaveBeenCalledWith(s.id);
});

test("Rename opens the map's dialog and announces the echoed title", async () => {
  const s = sess("claude");
  vi.spyOn(api, "rename").mockResolvedValue({ id: s.id, title: "Renamed" });
  mount(s, terminalHead(), false);
  await userEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Rename session" }));
  const dialog = screen.getByRole("dialog");
  const field = within(dialog).getByRole("textbox");
  await userEvent.clear(field);
  await userEvent.type(field, "Renamed{Enter}");
  await waitFor(() => expect(announced).toEqual([{ key: s.id, patch: { title: "Renamed" } }]));
  expect(api.rename).toHaveBeenCalledWith(s.id, "Renamed");
});

test("Archive announces, then leaves the pane, as archiving the open session from the sidebar does", async () => {
  const s = sess("claude");
  vi.spyOn(api, "archive").mockResolvedValue({ id: s.id, archived: true });
  mount(s, terminalHead(), false);
  await userEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Archive session" }));
  await waitFor(() => expect(screen.getByTestId("left-the-pane")).toBeInTheDocument());
  expect(announced).toEqual([{ key: s.id, patch: { archived: true } }]);
});

test("a refused archive stays on the pane and announces nothing", async () => {
  const s = sess("claude");
  vi.spyOn(api, "archive").mockRejectedValue(new Error("nope"));
  // The rejection surfaces through the menu's own `runBusy`; nothing here may swallow or act on it.
  const unhandled = vi.fn();
  process.on("unhandledRejection", unhandled);
  mount(s, terminalHead(), false);
  await userEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Archive session" }));
  await act(async () => {
    await Promise.resolve();
  });
  process.off("unhandledRejection", unhandled);
  expect(screen.queryByTestId("left-the-pane")).toBeNull();
  expect(announced).toEqual([]);
  expect(trigger()).toBeInTheDocument();
});

test("Unarchive stays on the pane", async () => {
  const s = sess("claude", { archived: true });
  vi.spyOn(api, "unarchive").mockResolvedValue({ id: s.id, archived: false });
  mount(s, terminalHead(), false);
  await userEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Unarchive session" }));
  await waitFor(() => expect(announced).toEqual([{ key: s.id, patch: { archived: false } }]));
  expect(screen.queryByTestId("left-the-pane")).toBeNull();
});

test("the pane's menu is anchored, never the phone sheet", async () => {
  mount(sess("claude"), terminalHead(), true);
  expect(await openMenu()).toHaveAttribute("data-menu-placement", "anchored");
});
