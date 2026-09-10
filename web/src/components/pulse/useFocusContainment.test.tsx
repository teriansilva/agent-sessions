/** Tab containment around a panel whose focus does not start on a tab stop (#940 review 5).
 *
 *  This is pinned here rather than in a browser because the browser tests cannot isolate it. The
 *  defect appeared through `MoveToProjectModal`, which focused its own `tabIndex={-1}` container
 *  on mount; that modal now focuses its Cancel button instead, and with a real control as the
 *  initial target the identity comparison this replaced would pass every one of those specs. The
 *  containment fix and the modal fix each independently prevent the reported symptom, so a test
 *  that goes through the modal proves only that at least one of them is present.
 *
 *  The hook is shared, and the next surface to focus a container will not be that modal — the
 *  mission console's overflow menu already focuses a `tabIndex={-1}` wrapper for a good reason
 *  (which item is first depends on the mission's state, and landing on a destructive action in
 *  some states and not others is worse than landing on neither). So the generalisation is the
 *  thing worth keeping, and it is worth asking the hook about directly.
 */
import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { expect, test } from "vitest";

import { useFocusContainment } from "./useModalDrawer";

/** A panel with tab stops on either side of it, so "focus left the panel" is a reachable outcome
 *  rather than something the fixture makes impossible. */
function Panel() {
  const panelRef = useRef<HTMLDivElement | null>(null);
  useFocusContainment({ active: true, panelRef });
  return (
    <div>
      <button data-testid="before">before</button>
      <div ref={panelRef} tabIndex={-1} data-testid="panel">
        <button data-testid="first">first</button>
        <button data-testid="last">last</button>
      </div>
      <button data-testid="after">after</button>
    </div>
  );
}

/** Press Tab (or Shift+Tab) at the document, the way the hook listens for it, and report whether
 *  it was cancelled — cancelling is how the hook places focus instead of the browser. */
function press(shift: boolean): boolean {
  const e = new KeyboardEvent("keydown", {
    key: "Tab",
    shiftKey: shift,
    bubbles: true,
    cancelable: true,
  });
  document.dispatchEvent(e);
  return e.defaultPrevented;
}

test("Shift+Tab from the panel's own non-tabbable container wraps to the LAST item", () => {
  render(<Panel />);
  // The state the defect lived in: focus is INSIDE the panel and is not one of its tab stops.
  screen.getByTestId("panel").focus();
  expect(document.activeElement).toBe(screen.getByTestId("panel"));

  expect(press(true)).toBe(true);
  // Backwards from "nowhere in the cycle" is the end of it. Before the fix this matched neither
  // `first` nor `last`, was not intercepted, and the browser walked focus out of the panel.
  expect(document.activeElement).toBe(screen.getByTestId("last"));
});

test("Tab from that same container goes to the FIRST item", () => {
  render(<Panel />);
  screen.getByTestId("panel").focus();
  expect(press(false)).toBe(true);
  expect(document.activeElement).toBe(screen.getByTestId("first"));
});

test("an ordinary position in the cycle is left to the browser", () => {
  // The control: containment places the press only at the ENDS. Intercepting in the middle would
  // make the panel's own order unnavigable, and would pass the two tests above for free.
  render(<Panel />);
  screen.getByTestId("first").focus();
  expect(press(false)).toBe(false);
  expect(document.activeElement).toBe(screen.getByTestId("first"));
});

test("the ends still wrap, which is what containment meant before any of this", () => {
  render(<Panel />);
  screen.getByTestId("last").focus();
  expect(press(false)).toBe(true);
  expect(document.activeElement).toBe(screen.getByTestId("first"));

  screen.getByTestId("first").focus();
  expect(press(true)).toBe(true);
  expect(document.activeElement).toBe(screen.getByTestId("last"));
});
