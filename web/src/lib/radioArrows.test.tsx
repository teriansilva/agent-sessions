import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, test } from "vitest";
import { onRadioGroupKeyDown, rovingTabIndex } from "./radioArrows";

function Group() {
  const [v, setV] = useState("a");
  return (
    <div role="radiogroup" aria-label="g" onKeyDown={onRadioGroupKeyDown}>
      {["a", "b", "c"].map((id) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={v === id}
          disabled={id === "b" ? false : undefined}
          onClick={() => setV(id)}
        >
          {id}
        </button>
      ))}
    </div>
  );
}

test("arrows move focus AND selection, wrapping at the ends (#1494)", async () => {
  render(<Group />);
  screen.getByRole("radio", { name: "a" }).focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("radio", { name: "b" })).toHaveFocus();
  expect(screen.getByRole("radio", { name: "b" })).toBeChecked();
  await userEvent.keyboard("{End}");
  expect(screen.getByRole("radio", { name: "c" })).toBeChecked();
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("radio", { name: "a" })).toBeChecked();
  await userEvent.keyboard("{ArrowUp}");
  expect(screen.getByRole("radio", { name: "c" })).toBeChecked();
});

test("roving tabindex: one Tab stop per group — the checked radio, else the first", () => {
  expect([0, 1, 2].map((i) => rovingTabIndex(i === 1, i, true))).toEqual([-1, 0, -1]);
  expect([0, 1, 2].map((i) => rovingTabIndex(false, i, false))).toEqual([0, -1, -1]);
});
