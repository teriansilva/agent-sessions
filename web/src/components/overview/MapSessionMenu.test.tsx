import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { MapSessionMenu } from "./MapSessionMenu";
import type { Session } from "../../types/api";

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

function renderMenu(omit: ReadonlySet<string> | undefined) {
  return render(
    <MemoryRouter>
      <MapSessionMenu
        session={SESSION}
        anchor={{ element: document.body }}
        handlers={handlers}
        onMenuClose={() => {}}
        onDone={() => {}}
        reviewInFlight={false}
        paneItems={omit ? [] : undefined}
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
