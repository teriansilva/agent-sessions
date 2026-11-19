import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import { HeadActions, type HeadAction } from "./HeadActions";

/** `foldInto: "external"` publishes its overflow AND its full action list to the host's refs,
 *  and clears both on unmount (#1329) — RuntimeGate swaps this pane out when a chat/api roster
 *  entry arrives, and a stale list would make the merged menu omit session actions forever. */

const action = (id: string): HeadAction => ({
  id,
  label: id,
  aria: id,
  title: id,
  icon: null,
  run: () => {},
});

const props = {
  className: "c",
  btnClassName: "b",
  labelClassName: "l",
  foldInto: "external" as const,
};

test("publishes the full list to the host and clears it on unmount (#1329)", () => {
  const overflowRef = { current: [] as HeadAction[] };
  const allRef = { current: [] as HeadAction[] };
  const { unmount } = render(
    <HeadActions
      {...props}
      actions={[action("recap"), action("handoff")]}
      overflowRef={overflowRef}
      allRef={allRef}
    />,
  );
  expect(allRef.current.map((a) => a.id)).toEqual(["recap", "handoff"]);

  unmount();
  expect(allRef.current).toEqual([]);
  expect(overflowRef.current).toEqual([]);
});

// ---- an action with `menu` (#2173): a chip with its own menu, or a labelled group once folded ----

const withMenu = (runs: Record<string, (HTMLElement | null | undefined)[]>): HeadAction => ({
  ...action("share"),
  label: "Share",
  aria: "Share this session",
  run: () => {
    throw new Error("an action with a menu never runs itself");
  },
  menu: [
    {
      ...action("share-public"),
      label: "Public…",
      aria: "Share this session publicly",
      disabled: true,
      hint: "Not available for terminal sessions",
      run: (t) => (runs["share-public"] ??= []).push(t),
    },
    {
      ...action("share-peer"),
      label: "Peer…",
      aria: "Share this session with a paired instance",
      run: (t) => (runs["share-peer"] ??= []).push(t),
    },
  ],
});

const own = { className: "c", btnClassName: "b", labelClassName: "l" };

test("as a chip it opens a menu of its items; an item runs with the CHIP, and Esc returns to it", async () => {
  const runs: Record<string, (HTMLElement | null | undefined)[]> = {};
  render(<HeadActions {...own} actions={[action("recap"), withMenu(runs)]} />);
  const chip = screen.getByRole("button", { name: "Share this session" });
  expect(chip).toHaveAttribute("aria-haspopup", "menu");
  expect(chip).toHaveAttribute("aria-expanded", "false");
  expect(chip).not.toHaveAttribute("aria-pressed");

  await userEvent.click(chip);
  expect(chip).toHaveAttribute("aria-expanded", "true");
  const menu = screen.getByRole("menu", { name: "Share this session" });
  expect(within(menu).getAllByRole("menuitem").map((m) => m.getAttribute("aria-label"))).toEqual([
    "Share this session publicly",
    "Share this session with a paired instance",
  ]);

  // The disabled item is listed with its reason and opens nothing; the menu stays.
  const off = within(menu).getByRole("menuitem", { name: "Share this session publicly" });
  expect(off).toHaveAttribute("aria-disabled", "true");
  expect(off).toHaveTextContent("Not available for terminal sessions");
  await userEvent.click(off);
  expect(runs["share-public"]).toBeUndefined();
  expect(screen.getByRole("menu", { name: "Share this session" })).toBeInTheDocument();

  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(chip).toHaveFocus();

  await userEvent.click(chip);
  await userEvent.click(screen.getByRole("menuitem", { name: "Share this session with a paired instance" }));
  expect(screen.queryByRole("menu")).toBeNull();
  // The persistent chip, never the menu item that just unmounted.
  expect(runs["share-peer"]).toEqual([chip]);
  expect(document.contains(chip)).toBe(true);
});

test("collapsed (the ≤800px Actions menu): the group label once, then its items as menu items", async () => {
  const runs: Record<string, (HTMLElement | null | undefined)[]> = {};
  render(
    <HeadActions {...own} collapsed actions={[action("recap"), withMenu(runs), action("stop")]} />,
  );
  // No chip, so no chip menu: everything is behind the one trigger.
  expect(document.querySelectorAll("[data-head-action]")).toHaveLength(0);
  const trigger = screen.getByTestId("head-actions-menu");
  await userEvent.click(trigger);
  const menu = screen.getByRole("menu", { name: "Actions for this session" });
  expect(within(menu).getAllByRole("menuitem").map((m) => m.getAttribute("aria-label"))).toEqual([
    "recap",
    "Share this session publicly",
    "Share this session with a paired instance",
    "stop",
  ]);
  const groups = menu.querySelectorAll("[data-menu-group]");
  expect(groups).toHaveLength(1);
  expect(groups[0]).toHaveTextContent("Share");
  expect(groups[0]).toHaveAttribute("aria-hidden", "true");
  // The group is no menu item: there is no "Share" row to press, and no nested menu.
  expect(within(menu).queryByRole("menuitem", { name: "Share this session" })).toBeNull();
  // Ruled off on both sides, and no doubled rule.
  expect(menu.querySelectorAll('[role="separator"]')).toHaveLength(2);

  const off = within(menu).getByRole("menuitem", { name: "Share this session publicly" });
  expect(off).toBeDisabled();
  expect(off).toHaveTextContent("Not available for terminal sessions");

  await userEvent.click(within(menu).getByRole("menuitem", { name: "Share this session with a paired instance" }));
  expect(screen.queryByRole("menu")).toBeNull();
  // The Actions trigger, which outlives the menu.
  expect(runs["share-peer"]).toEqual([trigger]);
});

test('folded into the "…": the same group, once', async () => {
  // jsdom lays nothing out, so the measured fold is driven through a ResizeObserver stub and
  // element widths: a 200px bar holds one 90px chip and the "…", not two chips.
  const realRO = globalThis.ResizeObserver;
  const realOffset = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
  const realClient = Object.getOwnPropertyDescriptor(Element.prototype, "clientWidth");
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, get: () => 90 });
  Object.defineProperty(Element.prototype, "clientWidth", { configurable: true, get: () => 200 });
  try {
    const runs: Record<string, (HTMLElement | null | undefined)[]> = {};
    render(
      <div>
        <HeadActions {...own} actions={[action("recap"), withMenu(runs)]} />
      </div>,
    );
    expect(
      Array.from(document.querySelectorAll("[data-head-action]")).map((b) =>
        b.getAttribute("data-head-action"),
      ),
    ).toEqual(["recap"]);
    const more = screen.getByRole("button", { name: "More session actions" });
    await userEvent.click(more);
    const menu = screen.getByRole("menu", { name: "More session actions" });
    expect(menu.querySelectorAll("[data-menu-group]")).toHaveLength(1);
    expect(within(menu).getAllByRole("menuitem").map((m) => m.textContent)).toEqual([
      "Public…Not available for terminal sessions",
      "Peer…",
    ]);
    // The only folded entry: no rule before or after its group.
    expect(menu.querySelectorAll('[role="separator"]')).toHaveLength(0);
    await userEvent.click(within(menu).getByRole("menuitem", { name: /paired instance/ }));
    expect(runs["share-peer"]).toEqual([more]);
  } finally {
    globalThis.ResizeObserver = realRO;
    if (realOffset) Object.defineProperty(HTMLElement.prototype, "offsetWidth", realOffset);
    if (realClient) Object.defineProperty(Element.prototype, "clientWidth", realClient);
  }
});

test("foldInto external publishes the action whole, menu and all, for the host to draw as a group", () => {
  const overflowRef = { current: [] as HeadAction[] };
  render(<HeadActions {...props} collapsed actions={[withMenu({})]} overflowRef={overflowRef} />);
  expect(overflowRef.current.map((a) => a.id)).toEqual(["share"]);
  expect(overflowRef.current[0].menu?.map((m) => m.id)).toEqual(["share-public", "share-peer"]);
  // No trigger and no menu of its own (#1109).
  expect(screen.queryByRole("button")).toBeNull();
});
