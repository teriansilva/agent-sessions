import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { UploadQueue } from "./UploadQueue";
import type { UploadsApi } from "./useUploads";

/** The name-taken prompt (#807) on THE dialog chrome (#1531): the behaviour is unchanged — a modal
 *  alertdialog, focus on the SAFE choice, Escape = Skip, Replace drawn as the destructive one. */
function uploads(over: Partial<UploadsApi> = {}): UploadsApi {
  const row = { id: "u1", relpath: "notes.md", size: 10, state: { kind: "collision", reason: "exists" } } as UploadsApi["rows"][number];
  return {
    rows: [row],
    busy: true,
    refusal: null,
    collision: { row, remaining: 2 },
    start: vi.fn(),
    refuse: vi.fn(),
    resolveCollision: vi.fn(),
    dismiss: vi.fn(),
    ...over,
  };
}

test("the name-taken prompt is a modal alertdialog with focus on Skip; Escape skips (#1531)", async () => {
  const u = uploads();
  render(<UploadQueue uploads={u} />);
  const dialog = screen.getByRole("alertdialog", { name: "A file with that name already exists" });
  expect(dialog).toHaveAttribute("aria-modal", "true");
  expect(dialog).toHaveTextContent("Nothing has been overwritten");
  expect(screen.getByRole("button", { name: "Skip" })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(u.resolveCollision).toHaveBeenCalledWith("skip", false);
});

test("Replace is the destructive action and is styled apart from the safe ones (#1531)", async () => {
  const u = uploads();
  render(<UploadQueue uploads={u} />);
  const replace = screen.getByRole("button", { name: "Replace" });
  expect(replace.className).toMatch(/confirmBad/);
  expect(screen.getByRole("button", { name: "Skip" }).className).toMatch(/confirmGhost/);
  await userEvent.click(replace);
  expect(u.resolveCollision).toHaveBeenCalledWith("replace", false);
});
