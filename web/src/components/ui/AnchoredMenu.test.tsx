/** AnchoredMenu's two presentations (#1531 Phase 3): the anchored desktop menu, unchanged, and the
 *  ≤800px bottom sheet every menu becomes on a phone. The sheet keeps the menu contract (role,
 *  arrow keys, Esc, focus return) and adds what a modal sheet owes: a scrim that closes it, Cancel,
 *  an inert background, and Tab that stays inside instead of leaving. */
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AnchoredMenu } from "./AnchoredMenu";

function viewport(mobile: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: mobile && q.includes("max-width: 800px"),
    media: q,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  // @ts-expect-error -- jsdom has no matchMedia of its own
  delete window.matchMedia;
  document.getElementById("root")?.remove();
});

/** Mount inside a `#root`, as the app is, so the sheet's isolation has something to make inert. */
function mount(onPick = vi.fn()) {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  render(
    <>
      <button type="button">Before</button>
      <AnchoredMenu
        label="Session menu"
        trigger={<span>⋯</span>}
        triggerClassName="t"
        triggerTestId="trigger"
        menuTestId="panel"
        focus="first-item"
        classes={{ wrap: "w", panel: "p", items: "i" }}
      >
        {(close) => (
          <>
            <button type="button" role="menuitem" onClick={() => (close(), onPick("one"))}>
              One
            </button>
            <button type="button" role="menuitem" onClick={() => (close(), onPick("two"))}>
              Two
            </button>
          </>
        )}
      </AnchoredMenu>
    </>,
    { container: root },
  );
  return onPick;
}

test("desktop: an anchored menu in place — arrows move, Tab leaves, no scrim, nothing inert", async () => {
  viewport(false);
  mount();
  await userEvent.click(screen.getByTestId("trigger"));
  const panel = screen.getByTestId("panel");
  expect(panel).not.toHaveAttribute("data-sheet");
  expect(screen.queryByTestId("panel-scrim")).toBeNull();
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  expect(screen.getByRole("menuitem", { name: "One" })).toHaveFocus();
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "Two" })).toHaveFocus();
  expect(document.getElementById("root")).not.toHaveAttribute("inert");
  await userEvent.keyboard("{Tab}");
  expect(screen.queryByTestId("panel")).toBeNull();
});

test("phone: a bottom sheet in <body> with a title, a scrim and Cancel; the background is inert", async () => {
  viewport(true);
  mount();
  await userEvent.click(screen.getByTestId("trigger"));
  const panel = screen.getByTestId("panel");
  expect(panel).toHaveAttribute("data-sheet");
  // Portalled out of #root: the sheet is not inside the app it isolates.
  expect(document.getElementById("root")!.contains(panel)).toBe(false);
  expect(screen.getByTestId("panel-scrim")).toBeInTheDocument();
  expect(panel).toHaveTextContent("Session menu");
  expect(screen.getByRole("menu", { name: "Session menu" })).toBeInTheDocument();
  expect(document.getElementById("root")).toHaveAttribute("inert");
  // A MODAL sheet is a named modal dialog around the menu (Hermes on #1560): the menu and its
  // Cancel are both inside it.
  const sheet = screen.getByRole("dialog", { name: "Session menu" });
  expect(sheet).toBe(panel);
  expect(sheet).toHaveAttribute("aria-modal", "true");
  expect(sheet).toContainElement(screen.getByRole("menu", { name: "Session menu" }));
  expect(sheet).toContainElement(screen.getByRole("button", { name: "Cancel" }));
  // The arrow-key contract is the same on a phone.
  expect(screen.getByRole("menuitem", { name: "One" })).toHaveFocus();
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "Two" })).toHaveFocus();
});

test("phone: Tab stays inside the sheet rather than closing it", async () => {
  viewport(true);
  mount();
  await userEvent.click(screen.getByTestId("trigger"));
  await userEvent.keyboard("{Tab}");
  expect(screen.getByTestId("panel")).toBeInTheDocument();
  expect(screen.getByTestId("panel").contains(document.activeElement)).toBe(true);
});

test("phone: Esc, Cancel and a press on the scrim each close it and focus returns to the trigger", async () => {
  viewport(true);
  mount();
  const trigger = screen.getByTestId("trigger");

  await userEvent.click(trigger);
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByTestId("panel")).toBeNull();
  // The restore waits a frame (useModalDrawer: a tap is still settling focus on close).
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(document.getElementById("root")).not.toHaveAttribute("inert");

  await userEvent.click(trigger);
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByTestId("panel")).toBeNull();
  await waitFor(() => expect(trigger).toHaveFocus());

  await userEvent.click(trigger);
  await act(async () => {
    screen.getByTestId("panel-scrim").dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  });
  expect(screen.queryByTestId("panel")).toBeNull();
});

test("phone: an item acts and closes the sheet", async () => {
  viewport(true);
  const onPick = mount();
  await userEvent.click(screen.getByTestId("trigger"));
  await userEvent.click(screen.getByRole("menuitem", { name: "Two" }));
  expect(onPick).toHaveBeenCalledWith("two");
  expect(screen.queryByTestId("panel")).toBeNull();
});

test("the items function is handed the trigger, read at event time (a portalled sheet has no DOM path back)", async () => {
  viewport(true);
  let got: HTMLElement | null = null;
  render(
    <AnchoredMenu
      label="M"
      trigger="⋯"
      triggerClassName="t"
      triggerTestId="trigger"
      classes={{ wrap: "w", panel: "p", items: "i" }}
    >
      {(close, trigger) => (
        <button type="button" role="menuitem" onClick={() => ((got = trigger()), close())}>
          Act
        </button>
      )}
    </AnchoredMenu>,
  );
  await userEvent.click(screen.getByTestId("trigger"));
  await userEvent.click(screen.getByRole("menuitem", { name: "Act" }));
  expect(got).toBe(screen.getByTestId("trigger"));
});

test("a trigger label and title of its own name the trigger; the menu and the sheet title keep `label` (#1658)", async () => {
  viewport(true);
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  render(
    <AnchoredMenu
      label="Relay connection"
      triggerLabel="Relay connection: linked — onyx-9780, 3:57:50 left"
      triggerTitle="Relay connection: linked — onyx-9780, 3:57:50 left"
      trigger={<span>●</span>}
      triggerClassName="t"
      triggerTestId="trigger"
      menuTestId="panel"
      classes={{ wrap: "w", panel: "p", items: "i" }}
    >
      <button type="button" role="menuitem">Sign out</button>
    </AnchoredMenu>,
    { container: root },
  );
  const trigger = screen.getByTestId("trigger");
  expect(trigger).toHaveAccessibleName("Relay connection: linked — onyx-9780, 3:57:50 left");
  expect(trigger).toHaveAttribute("title", "Relay connection: linked — onyx-9780, 3:57:50 left");
  await userEvent.click(trigger);
  expect(screen.getByRole("dialog", { name: "Relay connection" })).toBeInTheDocument();
  expect(screen.getByRole("menu", { name: "Relay connection" })).toBeInTheDocument();
});
