import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { RenameProjectModal } from "./RenameProjectModal";

function renderModal(overrides: Partial<Parameters<typeof RenameProjectModal>[0]> = {}) {
  const onCancel = vi.fn();
  const onSave = vi.fn();
  const trigger = document.createElement("button");
  trigger.id = "trigger";
  trigger.textContent = "Open";
  document.body.appendChild(trigger);
  trigger.focus();
  const result = render(
    <RenameProjectModal
      cwd="/home/u/alpha"
      initialName=""
      onCancel={onCancel}
      onSave={onSave}
      returnFocusTo={trigger}
      {...overrides}
    />,
  );
  return { ...result, onCancel, onSave, trigger };
}

test("focuses the input on open + announces dialog role/labelling (#174)", () => {
  renderModal({ initialName: "Alpha" });
  const dialog = screen.getByRole("dialog", { name: /rename project/i });
  expect(dialog).toBeInTheDocument();
  // The input is focused at mount and pre-filled with the current name (selected for replace).
  const input = screen.getByRole("textbox", { name: /custom name for/i });
  expect(input).toHaveFocus();
  expect(input).toHaveValue("Alpha");
});

test("Enter in the input saves; Escape cancels (#174)", async () => {
  const { onSave, onCancel } = renderModal();
  const input = screen.getByRole("textbox", { name: /custom name for/i });
  await userEvent.type(input, "Alpha 1{Enter}");
  expect(onSave).toHaveBeenCalledWith("Alpha 1");

  await userEvent.keyboard("{Escape}");
  expect(onCancel).toHaveBeenCalled();
});

test("Cancel button cancels (no save); Save button saves (#174)", async () => {
  const { onSave, onCancel } = renderModal({ initialName: "Old" });
  const input = screen.getByRole("textbox", { name: /custom name for/i });
  await userEvent.clear(input);
  await userEvent.type(input, "New");

  await userEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
  expect(onCancel).toHaveBeenCalled();
  expect(onSave).not.toHaveBeenCalled();

  // The dialog re-renders for the next assertion (cancel does not unmount in this test setup),
  // so click Save and verify the trimmed name is forwarded.
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(onSave).toHaveBeenCalledWith("New");
});

test("clicking the backdrop cancels without saving (#174)", async () => {
  const { onSave, onCancel, baseElement } = renderModal();
  const backdrop = baseElement.querySelector(`[class*="backdrop"]`) as HTMLElement;
  expect(backdrop).toBeTruthy();
  await userEvent.click(backdrop);
  expect(onCancel).toHaveBeenCalled();
  expect(onSave).not.toHaveBeenCalled();
});

test("clicking inside the dialog does NOT cancel (#174)", async () => {
  const { onCancel } = renderModal();
  const dialog = screen.getByRole("dialog");
  await userEvent.click(dialog);
  // Clicks inside don't bubble to the backdrop's cancel.
  expect(onCancel).not.toHaveBeenCalled();
});

test("save with empty input forwards an empty string (clears the custom name) (#174)", async () => {
  const { onSave } = renderModal({ initialName: "Old" });
  const input = screen.getByRole("textbox", { name: /custom name for/i });
  await userEvent.clear(input);
  await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
  expect(onSave).toHaveBeenCalledWith("");
});

test("focus returns to the trigger on unmount (#174 a11y)", () => {
  const { unmount, trigger } = renderModal();
  // Move focus elsewhere so we can clearly see it return.
  (document.activeElement as HTMLElement | null)?.blur?.();
  unmount();
  expect(document.activeElement).toBe(trigger);
});
