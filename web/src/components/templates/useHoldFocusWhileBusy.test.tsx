/** A modal whose request disables its controls holds focus itself (#1531, Hermes on #1560). */
import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import { expect, test } from "vitest";

import { useHoldFocusWhileBusy } from "./useHoldFocusWhileBusy";

function Dialog({ busy, keepOneLive = false }: { busy: boolean; keepOneLive?: boolean }) {
  const panel = useRef<HTMLDivElement>(null);
  useHoldFocusWhileBusy(panel, busy);
  return (
    <div ref={panel} role="dialog" aria-label="Confirm" tabIndex={-1}>
      <button type="button" disabled={busy}>
        Confirm
      </button>
      <button type="button" disabled={busy && !keepOneLive}>
        Close
      </button>
    </div>
  );
}

test("busy disabling the focused control: the dialog takes focus, not the body", () => {
  const { rerender } = render(<Dialog busy={false} />);
  screen.getByRole("button", { name: "Confirm" }).focus();
  rerender(<Dialog busy />);
  expect(screen.getByRole("dialog")).toHaveFocus();
});

test("a control that stays live keeps its focus", () => {
  const { rerender } = render(<Dialog busy={false} keepOneLive />);
  screen.getByRole("button", { name: "Close" }).focus();
  rerender(<Dialog busy keepOneLive />);
  expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
});
