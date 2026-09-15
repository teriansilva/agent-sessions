import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Archive, Pencil, Sparkles } from "lucide-react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { MenuPopover, RowMenu, type RowMenuEntry } from "./RowMenu";

function entries(over: {
  onReview?: () => void;
  onRename?: () => void;
  onArchive?: () => void;
  reviewDisabled?: boolean;
}): RowMenuEntry[] {
  return [
    {
      key: "review",
      label: "Review now",
      ariaLabel: "Review session now",
      icon: <Sparkles size={15} />,
      disabled: over.reviewDisabled,
      onSelect: over.onReview ?? (() => {}),
    },
    "separator",
    {
      key: "rename",
      label: "Rename",
      ariaLabel: "Rename session",
      icon: <Pencil size={15} />,
      onSelect: over.onRename ?? (() => {}),
    },
    {
      key: "archive",
      label: "Archive",
      ariaLabel: "Archive session",
      icon: <Archive size={15} />,
      onSelect: over.onArchive ?? (() => {}),
    },
  ];
}

test("menu is closed until the trigger is clicked; trigger reflects expanded state", async () => {
  const user = userEvent.setup();
  render(<RowMenu items={entries({})} title="t" />);
  const trigger = screen.getByRole("button", { name: "Session actions" });
  expect(trigger).toHaveAttribute("aria-haspopup", "menu");
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();

  await user.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  // Portaled to <body> (via the bottom-sheet wrapper) so the sidebar scroll container
  // can't clip it — menu → sheet wrapper → body.
  const menu = screen.getByRole("menu", { name: "Session actions" });
  expect(menu.parentElement?.parentElement).toBe(document.body);
  // Menu-button pattern: the first item takes focus on open.
  expect(
    screen.getByRole("menuitem", { name: "Review session now" }),
  ).toHaveFocus();
});

test("ArrowUp/Down cycle with wrap, Home/End jump (#384 keyboard)", async () => {
  const user = userEvent.setup();
  render(<RowMenu items={entries({})} />);
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  const [review, rename, archive] = screen.getAllByRole("menuitem");
  expect(review).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(rename).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(archive).toHaveFocus();
  await user.keyboard("{ArrowDown}"); // wraps
  expect(review).toHaveFocus();
  await user.keyboard("{ArrowUp}"); // wraps backwards
  expect(archive).toHaveFocus();
  await user.keyboard("{Home}");
  expect(review).toHaveFocus();
  await user.keyboard("{End}");
  expect(archive).toHaveFocus();
});

test("Escape closes and returns focus to the trigger", async () => {
  const user = userEvent.setup();
  render(<RowMenu items={entries({})} />);
  const trigger = screen.getByRole("button", { name: "Session actions" });
  await user.click(trigger);
  expect(screen.getByRole("menu")).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("outside click closes; clicking the trigger again toggles", async () => {
  const user = userEvent.setup();
  render(
    <div>
      <button type="button">outside</button>
      <RowMenu items={entries({})} />
    </div>,
  );
  const trigger = screen.getByRole("button", { name: "Session actions" });
  await user.click(trigger);
  expect(screen.getByRole("menu")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "outside" }));
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  // toggle: open then close via the trigger itself (no outside-close race)
  await user.click(trigger);
  expect(screen.getByRole("menu")).toBeInTheDocument();
  await user.click(trigger);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("scrolling the surrounding container closes the menu (anchor moved)", async () => {
  const user = userEvent.setup();
  render(
    <div data-testid="scrollbox">
      <RowMenu items={entries({})} />
    </div>,
  );
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  expect(screen.getByRole("menu")).toBeInTheDocument();
  fireEvent.scroll(screen.getByTestId("scrollbox"));
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("selecting an item dispatches its action, closes, and restores trigger focus", async () => {
  const user = userEvent.setup();
  const onArchive = vi.fn();
  render(<RowMenu items={entries({ onArchive })} />);
  const trigger = screen.getByRole("button", { name: "Session actions" });
  await user.click(trigger);
  await user.click(screen.getByRole("menuitem", { name: "Archive session" }));
  expect(onArchive).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

test("Enter activates the focused item (keyboard dispatch)", async () => {
  const user = userEvent.setup();
  const onRename = vi.fn();
  render(<RowMenu items={entries({ onRename })} />);
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  await user.keyboard("{ArrowDown}"); // → Rename
  await user.keyboard("{Enter}");
  expect(onRename).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

test("a disabled item stays in the menu (aria-disabled) but never dispatches", async () => {
  const user = userEvent.setup();
  const onReview = vi.fn();
  render(<RowMenu items={entries({ onReview, reviewDisabled: true })} />);
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  const item = screen.getByRole("menuitem", { name: "Review session now" });
  expect(item).toHaveAttribute("aria-disabled", "true");
  await user.click(item);
  expect(onReview).not.toHaveBeenCalled();
  // Menu stays open — a dead click on a disabled item shouldn't dismiss the menu.
  expect(screen.getByRole("menu")).toBeInTheDocument();
});

test("separators render with role=separator between groups", async () => {
  const user = userEvent.setup();
  render(<RowMenu items={entries({})} />);
  await user.click(screen.getByRole("button", { name: "Session actions" }));
  expect(screen.getByRole("separator")).toBeInTheDocument();
});

// --- MenuPopover at a pointer (#968) ------------------------------------------------------------
// jsdom lays nothing out, so the menu's measured size is stubbed; the viewport is jsdom's 1024×768.
describe("MenuPopover — point anchor", () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(100);
  });
  afterEach(() => vi.restoreAllMocks());

  const placed = () => {
    const menu = screen.getByRole("menu", { name: "Session actions" });
    return {
      top: menu.style.getPropertyValue("--rm-top"),
      right: menu.style.getPropertyValue("--rm-right"),
    };
  };

  test("its top-left corner sits on the pointer", () => {
    render(
      <MenuPopover items={entries({})} anchor={{ point: { x: 100, y: 120 } }} onClose={() => {}} />,
    );
    // right = innerWidth − (x + menu width) = 1024 − 300
    expect(placed()).toEqual({ top: "120px", right: "724px" });
    expect(screen.getByRole("menuitem", { name: "Review session now" })).toHaveFocus();
  });

  test("near the bottom-right corner it flips left and up to stay on screen", () => {
    render(
      <MenuPopover items={entries({})} anchor={{ point: { x: 1000, y: 740 } }} onClose={() => {}} />,
    );
    // left = 1000 − 200 → right = 1024 − 1000; top = 740 − 100
    expect(placed()).toEqual({ top: "640px", right: "24px" });
  });

  test("a pointer without coordinates places it at the viewport edge, never at NaN", () => {
    render(
      <MenuPopover
        items={entries({})}
        anchor={{ point: { x: Number.NaN, y: Number.NaN } }}
        onClose={() => {}}
      />,
    );
    expect(placed()).toEqual({ top: "8px", right: "816px" });
  });

  test("an outside press closes it without refocus; a press on its owner does not", () => {
    const onClose = vi.fn();
    render(
      <div>
        <button type="button">owner</button>
        <button type="button">elsewhere</button>
      </div>,
    );
    const owner = screen.getByRole("button", { name: "owner" });
    render(
      <MenuPopover
        items={entries({})}
        anchor={{ point: { x: 10, y: 10 } }}
        onClose={onClose}
        ownerRef={{ current: owner }}
      />,
    );
    fireEvent.pointerDown(owner);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByRole("button", { name: "elsewhere" }));
    expect(onClose).toHaveBeenCalledWith(false);
  });
});

describe("MenuPopover — element anchor", () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(100);
  });
  afterEach(() => vi.restoreAllMocks());

  const anchorAt = (left: number, top: number) => {
    const el = document.createElement("button");
    el.getBoundingClientRect = () =>
      ({ left, right: left + 30, top, bottom: top + 30, width: 30, height: 30, x: left, y: top }) as DOMRect;
    document.body.appendChild(el);
    return el;
  };
  const placed = () => {
    const menu = screen.getByRole("menu", { name: "Session actions" });
    return {
      top: menu.style.getPropertyValue("--rm-top"),
      right: menu.style.getPropertyValue("--rm-right"),
    };
  };

  test("right-aligned under its trigger, as the sidebar always had it", () => {
    render(<MenuPopover items={entries({})} anchor={{ element: anchorAt(970, 200) }} onClose={() => {}} />);
    // right = 1024 − trigger.right (1000); top = trigger.bottom + GAP
    expect(placed()).toEqual({ top: "234px", right: "24px" });
  });

  test("a trigger near the LEFT edge (a panned map chip) keeps the whole menu on screen (#968 review)", () => {
    render(<MenuPopover items={entries({})} anchor={{ element: anchorAt(100, 200) }} onClose={() => {}} />);
    // Unclamped, right = 1024 − 130 = 894 would put the menu's left edge at −70. Clamped so the
    // left edge sits at EDGE: right = 1024 − 8 − 200.
    expect(placed()).toEqual({ top: "234px", right: "816px" });
  });
});

test("onOpenChange mirrors open/close so the row can pin its action cluster visible", async () => {
  const user = userEvent.setup();
  const onOpenChange = vi.fn();
  render(<RowMenu items={entries({})} onOpenChange={onOpenChange} />);
  const trigger = screen.getByRole("button", { name: "Session actions" });
  await user.click(trigger);
  expect(onOpenChange).toHaveBeenLastCalledWith(true);
  await user.keyboard("{Escape}");
  expect(onOpenChange).toHaveBeenLastCalledWith(false);
});
