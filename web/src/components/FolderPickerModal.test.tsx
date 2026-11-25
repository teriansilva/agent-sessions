import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FolderPickerModal } from "./FolderPickerModal";

vi.mock("../lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    fsDirs: vi.fn(async (path?: string) => ({
      home: "/home/u",
      path: path ?? "/home/u",
      dirs: [{ name: "code", path: "/home/u/code" }],
    })),
    fsMkdir: vi.fn(),
  },
}));

/** The folder picker on THE dialog (#1531): portalled to <body> (a host's sidebar or pane is a
 *  containing block for `position: fixed`), focus contained and returned, Esc cancels, and every
 *  host still receives the chosen folder. */
let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  host.id = "host";
  document.body.appendChild(host);
});
afterEach(() => host.remove());

test("it renders portalled to <body>, outside the host that opened it (#1531)", async () => {
  render(<FolderPickerModal onPick={() => {}} onCancel={() => {}} />, { container: host });
  const dialog = await screen.findByRole("dialog", { name: "Choose a folder" });
  expect(host.contains(dialog)).toBe(false);
  expect(dialog).toHaveAttribute("aria-modal", "true");
});

test("Select hands the host the current folder (#1531)", async () => {
  const onPick = vi.fn();
  render(<FolderPickerModal onPick={onPick} onCancel={() => {}} />, { container: host });
  await userEvent.click(await screen.findByRole("button", { name: "code" }));
  await waitFor(() => expect(screen.getByRole("button", { name: /Select ~\/code/ })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: /Select ~\/code/ }));
  expect(onPick).toHaveBeenCalledWith("/home/u/code");
});

test("Esc cancels, and focus returns to the opener on close (#1531)", async () => {
  const opener = document.createElement("button");
  opener.textContent = "Browse";
  host.appendChild(opener);
  opener.focus();
  const onCancel = vi.fn();
  const view = render(<FolderPickerModal onPick={() => {}} onCancel={onCancel} returnFocusTo={opener} />, {
    container: document.body.appendChild(document.createElement("div")),
  });
  await screen.findByRole("button", { name: "code" });
  await userEvent.keyboard("{Escape}");
  expect(onCancel).toHaveBeenCalledTimes(1);
  view.unmount();
  expect(opener).toHaveFocus();
});

test("Tab stays inside the dialog (#1531)", async () => {
  const outside = document.createElement("button");
  outside.textContent = "outside";
  host.appendChild(outside);
  render(<FolderPickerModal onPick={() => {}} onCancel={() => {}} />, {
    container: document.body.appendChild(document.createElement("div")),
  });
  const dialog = await screen.findByRole("dialog");
  await screen.findByRole("button", { name: "code" });
  for (let i = 0; i < 12; i++) {
    await userEvent.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
  }
});
