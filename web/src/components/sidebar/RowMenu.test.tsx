import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Archive, Pencil, Sparkles } from "lucide-react";
import { expect, test, vi } from "vitest";
import { RowMenu, type RowMenuEntry } from "./RowMenu";

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
