/** The one ordering every mission ⋯ menu item that opens a dialog uses (#1968, #1969).
 *
 *  A desktop panel is closed through its trigger first; a phone's modal bottom sheet closes on a
 *  microtask and lifts its own `inert`, so the dialog is opened one task later, once the sheet is
 *  gone and the dialog can mark the page behind it inert itself. Both are mutation-checked here:
 *  dropping the trigger click leaves the desktop menu behind the dialog; dropping the deferral
 *  opens the phone dialog inside the sheet's inert window and leaves the page live. */
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { openDialogFromMenuItem } from "./menuDialog";

/** A minimal header: its ⋯ trigger and a menu item inside the header (desktop) or a portalled
 *  sheet (phone). */
function fixture({ sheet }: { sheet: boolean }) {
  document.body.innerHTML = `
    <div id="root" data-testid="mission-lifecycle">
      <button data-testid="mission-overflow" aria-expanded="true">⋯</button>
      <button data-testid="item">Artifacts…</button>
    </div>
  `;
  const trigger = document.querySelector<HTMLButtonElement>(
    '[data-testid="mission-overflow"]',
  )!;
  const item = document.querySelector<HTMLButtonElement>('[data-testid="item"]')!;
  if (sheet) {
    // `AnchoredMenu` portals a phone's sheet to <body>, outside the header; the header (and its
    // ⋯ trigger) stays in place.
    const sheetEl = document.createElement("div");
    sheetEl.setAttribute("data-sheet", "");
    sheetEl.append(item);
    document.body.append(sheetEl);
  }
  return { trigger, item };
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

test("desktop: closes the panel through its trigger, then opens the dialog in the same tick", () => {
  const { trigger, item } = fixture({ sheet: false });
  const clicked = vi.fn();
  trigger.addEventListener("click", clicked);
  const open = vi.fn();

  openDialogFromMenuItem(item, open);

  expect(clicked).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledWith(trigger);
});

test("phone: leaves the sheet's own close alone and opens the dialog after that close", () => {
  const { trigger, item } = fixture({ sheet: true });
  const clicked = vi.fn();
  trigger.addEventListener("click", clicked);
  const open = vi.fn();

  openDialogFromMenuItem(item, open);

  // Opening inside the sheet's inert window is the defect: nothing happens yet.
  expect(clicked).not.toHaveBeenCalled();
  expect(open).not.toHaveBeenCalled();

  vi.runAllTimers();

  expect(open).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledWith(trigger);
  expect(clicked).not.toHaveBeenCalled();
});
