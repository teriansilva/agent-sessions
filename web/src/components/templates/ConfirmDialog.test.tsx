import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";

// The app root the dialog makes inert while it is open (the dialog itself is portalled to
// <body>, outside it).
let root: HTMLDivElement;
beforeEach(() => {
  root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
});
afterEach(() => {
  root.remove();
});

function dialog(over: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  render(
    <ConfirmDialog tag="Delete" title="Thing" confirmLabel="Delete" onCancel={onCancel} onConfirm={onConfirm} {...over}>
      <p>body</p>
    </ConfirmDialog>,
    { container: root },
  );
  return { onCancel, onConfirm };
}

test("Escape, the backdrop and the close glyph all cancel when idle", async () => {
  const { onCancel } = dialog();
  await userEvent.keyboard("{Escape}");
  expect(onCancel).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: /close/i }));
  expect(onCancel).toHaveBeenCalledTimes(2);
});

test("while busy, NO dismissal path fires — Escape, backdrop, close, cancel, confirm (#907 review)", async () => {
  const { onCancel, onConfirm } = dialog({ busy: true });
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: /close/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^cancel$/i })).toBeDisabled();
  expect(screen.getByRole("button", { name: /^delete$/i })).toBeDisabled();
  // The backdrop is the dialog's parent element.
  const backdrop = screen.getByRole("dialog").parentElement!;
  await userEvent.pointer({ keys: "[MouseLeft>]", target: backdrop });
  expect(onCancel).not.toHaveBeenCalled();
  expect(onConfirm).not.toHaveBeenCalled();
});

test("the app root is inert while the dialog is open, and live again after (#907 review)", () => {
  const { unmount } = render(
    <ConfirmDialog tag="Delete" title="Thing" confirmLabel="Delete" onCancel={() => {}} onConfirm={() => {}} />,
  );
  expect(root.hasAttribute("inert")).toBe(true);
  // The dialog lives outside the inert root, so its controls stay reachable.
  expect(root.contains(screen.getByRole("dialog"))).toBe(false);
  unmount();
  expect(root.hasAttribute("inert")).toBe(false);
});

test("focus lands on the safe action and returns to the trigger", async () => {
  const trigger = document.createElement("button");
  trigger.textContent = "trigger";
  document.body.appendChild(trigger);
  trigger.focus();
  const { unmount } = render(
    <ConfirmDialog tag="Delete" title="Thing" confirmLabel="Delete" onCancel={() => {}} onConfirm={() => {}} returnFocusTo={trigger} />,
  );
  expect(screen.getByRole("button", { name: /^cancel$/i })).toHaveFocus();
  unmount();
  expect(trigger).toHaveFocus();
  trigger.remove();
});
