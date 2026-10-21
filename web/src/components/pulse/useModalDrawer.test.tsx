/** `useModalDrawer`'s focus containment, and its opt-out (#942).
 *
 *  The rest of the hook is exercised end-to-end by the drawers that use it — the bell's, the app
 *  shell's — in real browsers, which is where a focus trap belongs. What a browser test could NOT
 *  isolate is this one option, and that is exactly why it is pinned here: the menu that consumes
 *  it also handles Tab itself, so at the level of "what does the operator see" the two mechanisms
 *  produce the same outcome and either alone looks correct. The trap is only observable by asking
 *  the hook directly whether it moved focus.
 *
 *  Which is the whole point of the flag. `containFocus` exists so the NEXT non-modal surface
 *  cannot acquire a trap by omission — by passing an empty `inertRefs` and reasonably assuming
 *  that "isolate nothing" means "contain nothing". It does not; the two effects are independent.
 */
import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { expect, test } from "vitest";

import { useModalDrawer } from "./useModalDrawer";

function Panel({ containFocus }: { containFocus?: boolean }) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const firstRef = useRef<HTMLButtonElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  useModalDrawer({
    active: true,
    panelRef,
    initialFocusRef: firstRef,
    triggerRef,
    onClose: () => {},
    // Nothing is isolated — the case the flag exists to distinguish from.
    inertRefs: [],
    containFocus,
  });
  return (
    <div>
      <button ref={triggerRef} data-testid="outside">
        outside
      </button>
      <div ref={panelRef} data-testid="panel">
        <button ref={firstRef} data-testid="first">
          first
        </button>
        <button data-testid="last">last</button>
      </div>
    </div>
  );
}

/** Tab from the LAST item in the panel. With containment on, the hook cancels it and wraps to the
 *  first; with containment off it does nothing at all and the event is left to the browser. */
function tabFromLast(): boolean {
  screen.getByTestId("last").focus();
  const e = new KeyboardEvent("keydown", {
    key: "Tab",
    bubbles: true,
    cancelable: true,
  });
  document.dispatchEvent(e);
  return e.defaultPrevented;
}

test("by default Tab is CONTAINED — it wraps rather than leaving", () => {
  render(<Panel />);
  const prevented = tabFromLast();
  expect(prevented).toBe(true);
  expect(document.activeElement).toBe(screen.getByTestId("first"));
});

test("`containFocus: false` lets Tab leave — an empty `inertRefs` does NOT imply it", () => {
  render(<Panel containFocus={false} />);
  const prevented = tabFromLast();
  // Not cancelled, and focus was not pulled back to the top of the panel: the browser is left to
  // move on. This is the assertion that fails if the flag is dropped, or if it is wired to
  // `inertRefs` instead of standing on its own.
  expect(prevented).toBe(false);
  expect(document.activeElement).toBe(screen.getByTestId("last"));
});

test("the default is unchanged for a caller that names neither option", () => {
  // The regression that would matter most: every existing drawer passes no `containFocus`, so a
  // wrong default silently un-traps the bell and the app shell.
  render(<Panel />);
  expect(tabFromLast()).toBe(true);
});
