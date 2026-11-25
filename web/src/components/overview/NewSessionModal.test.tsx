/** #1648: the map's New session modal. The form inside is the page's own (tested there); this pins
 *  the dialog: Esc and the ✕ close it, a nested dialog takes Esc first, a create in flight holds it
 *  open, focus returns to the trigger, and the note says what Start does here. */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import { WorkspaceCommandsCtx, type WorkspaceCommands } from "../../app/workspaceWindows";
import { NewSessionModal } from "./NewSessionModal";

let reportBusy: (busy: boolean) => void = () => {};
vi.mock("../../routes/NewSessionLanding", () => ({
  NewSessionForm: ({ onBusy }: { onBusy?: (busy: boolean) => void }) => {
    reportBusy = (b) => onBusy?.(b);
    return <button type="button">form control</button>;
  },
}));

const ws = (mapReady: boolean) => ({ mapReady }) as unknown as WorkspaceCommands;

function open({ mapReady = true, trigger = null as HTMLElement | null } = {}) {
  const onClose = vi.fn();
  const r = render(
    <MemoryRouter>
      <WorkspaceCommandsCtx.Provider value={ws(mapReady)}>
        <NewSessionModal onClose={onClose} returnFocusTo={trigger} />
      </WorkspaceCommandsCtx.Provider>
    </MemoryRouter>,
  );
  return { onClose, ...r };
}

afterEach(() => {
  document.body.querySelectorAll("[data-nested]").forEach((n) => n.remove());
});

test("Esc and the ✕ close it; the dialog is labelled and modal", async () => {
  const user = userEvent.setup();
  const { onClose } = open();
  const dialog = screen.getByRole("dialog", { name: "New session // on the map" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  await user.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Close new session" }));
  expect(onClose).toHaveBeenCalledTimes(2);
});

test("a nested dialog opened above it takes Esc first", async () => {
  const user = userEvent.setup();
  const { onClose } = open();
  const nested = document.createElement("div");
  nested.setAttribute("aria-modal", "true");
  nested.setAttribute("data-nested", "");
  document.body.appendChild(nested);
  await user.keyboard("{Escape}");
  expect(onClose).not.toHaveBeenCalled();
  nested.remove();
  await user.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a create in flight holds it open: Esc, the ✕ and the backdrop do nothing", async () => {
  const user = userEvent.setup();
  const { onClose } = open();
  act(() => reportBusy(true));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("button", { name: "Close new session" })).toBeDisabled();
  expect(onClose).not.toHaveBeenCalled();
  act(() => reportBusy(false));
  await user.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("focus moves in on open and returns to the trigger on close", () => {
  const trigger = document.createElement("button");
  document.body.appendChild(trigger);
  const { unmount } = open({ trigger });
  expect(screen.getByRole("button", { name: "Close new session" })).toHaveFocus();
  unmount();
  expect(trigger).toHaveFocus();
  trigger.remove();
});

test("the note says where Start opens the session", () => {
  const { unmount } = open({ mapReady: true });
  expect(screen.getByTestId("new-session-modal-note")).toHaveTextContent("as a window here");
  unmount();
  open({ mapReady: false });
  expect(screen.getByTestId("new-session-modal-note")).toHaveTextContent("full screen");
});
