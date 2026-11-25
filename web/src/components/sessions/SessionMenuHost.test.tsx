import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { SessionMenuHost } from "./SessionMenuHost";
import type { Session } from "../../types/api";
import type { RowMenuEntry } from "../sidebar/RowMenu";
import { omittedSessionKeys, paneMenuEntries } from "./paneMenu";
import { shareHeadAction } from "./shareAction";

/** The merged window menu's omit rule (#1329): a pane-backed window drops the session
 *  twins of the actions its pane offers, while a window whose pane offers nothing (a chat/api
 *  runtime, or a chip's ⋯) keeps every session action. */

const SESSION: Session = {
  id: "claude:s1",
  engine: "claude",
  uuid: "s1",
  short_uuid: "s1",
  cwd: "/home/u/proj",
  project: { kind: "folder", id: "/home/u/proj", name: "/home/u/proj" },
  last_mtime: 1_700_000_000,
  first_user_message: "",
  title: "A session",
  sticky: false,
  archived: false,
  ai_summary: "",
  mission: null,
};

const handlers = {
  onToggleArchive: vi.fn(async () => {}),
  onToggleFavorite: vi.fn(async () => {}),
  onSetProject: vi.fn(async () => {}),
  onRename: vi.fn(async () => {}),
  onSetTag: vi.fn(async () => {}),
};

function renderMenu(omit: ReadonlySet<string> | undefined, paneItems?: RowMenuEntry[]) {
  return render(
    <MemoryRouter>
      <SessionMenuHost
        session={SESSION}
        anchor={{ element: document.body }}
        handlers={handlers}
        onMenuClose={() => {}}
        onDone={() => {}}
        reviewInFlight={false}
        paneItems={paneItems ?? (omit ? [] : undefined)}
        omit={omit}
      />
    </MemoryRouter>,
  );
}

test("a pane-backed window drops the session twins of the actions its pane offers (#1329)", () => {
  renderMenu(new Set(["brief", "handoff", "adopt-mission"]));
  const menu = screen.getByRole("menu");
  // The omitted twins are gone…
  expect(screen.queryByRole("menuitem", { name: /session brief/i })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: /adopt session to a mission/i })).toBeNull();
  // …while the row management run survives.
  expect(screen.getByRole("menuitem", { name: /rename session/i })).toBeInTheDocument();
  // No leading or doubled separator from the dropped group.
  expect(menu.firstElementChild?.getAttribute("role")).not.toBe("separator");
});

test("a window whose pane offers nothing omits nothing — each action stays reachable (#1329)", () => {
  renderMenu(undefined);
  const menu = screen.getByRole("menu");
  // Session brief is reachable exactly once — a chat/api window mounts no HeadActions, so
  // there is no Pane group to defer to and the session item must remain.
  expect(screen.getAllByRole("menuitem", { name: /session brief/i })).toHaveLength(1);
  expect(menu.querySelector("[data-menu-group='Pane']")).toBeNull();
});

// #2173: a window (or the session pane) whose pane FOLDED its Share action. The host builds the
// merged menu from the same two helpers `SessionWindow` and `PaneHeadActions` call, so this is
// the list a window's ⋯ shows: Share once, under Pane, and not a second time under Session.
test("a folded Share action is one Share group, under Pane, never also under Session (#2173)", () => {
  const share = shareHeadAction({ subject: "terminal", onPeer: () => {} });
  const recap = {
    id: "recap",
    label: "Recap",
    aria: "Open session brief",
    title: "Recap",
    icon: null,
    run: () => {},
  };
  // The pane offers Recap (a chip that fit) and Share (folded).
  renderMenu(omittedSessionKeys([recap, share]), paneMenuEntries([share], null));
  const menu = screen.getByRole("menu");
  const groups = Array.from(menu.querySelectorAll<HTMLElement>("[data-menu-group]")).map(
    (el) => el.dataset.menuGroup,
  );
  expect(groups).toEqual(["Session", "Pane", "Share"]);
  expect(screen.getAllByRole("menuitem", { name: "Share this session publicly" })).toHaveLength(1);
  expect(
    screen.getAllByRole("menuitem", { name: "Share this terminal's screen with a paired instance" }),
  ).toHaveLength(1);
  // Recap is a chip on the bar: named in neither group.
  expect(screen.queryByRole("menuitem", { name: /session brief/i })).toBeNull();
  // No rule straight after the Pane label, none doubled, none trailing.
  const kinds = Array.from(menu.children).map((el) =>
    el.getAttribute("role") === "separator" ? "sep" : el.hasAttribute("data-menu-group") ? "group" : "x",
  );
  kinds.forEach((k, i) => {
    if (k === "sep") expect(kinds[i + 1]).not.toBe("sep");
  });
});

test("a chip that fit (Share on the bar) leaves NO Share group in the menu (#2173)", () => {
  const share = shareHeadAction({ subject: "terminal", onPeer: () => {} });
  renderMenu(omittedSessionKeys([share]), paneMenuEntries([], null));
  const menu = screen.getByRole("menu");
  expect(menu.querySelector("[data-menu-group='Share']")).toBeNull();
  expect(menu.querySelector("[data-menu-group='Pane']")).toBeNull();
});
