import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ACCENT_PRESETS } from "../theme/accent";
import { ProjectColorPicker } from "./ProjectColorPicker";

test("offers the sixteen accent colours by name, plus None (#1508)", () => {
  render(<ProjectColorPicker label="Project colour" value="#38bdf8" onChange={() => {}} />);
  const group = screen.getByRole("radiogroup", { name: "Project colour" });
  const radios = screen.getAllByRole("radio");
  expect(group).toContainElement(radios[0]);
  expect(radios.map((r) => r.getAttribute("aria-label") ?? r.textContent)).toEqual([
    ...ACCENT_PRESETS.map((p) => p.label),
    "None",
  ]);
  expect(screen.getByRole("radio", { name: "Sky" })).toHaveAttribute("aria-checked", "true");
  // One Tab stop: the checked swatch.
  expect(radios.filter((r) => r.tabIndex === 0)).toEqual([screen.getByRole("radio", { name: "Sky" })]);
});

test("a colour outside the sixteen is kept as the checked Current swatch", () => {
  render(<ProjectColorPicker label="Project colour" value="#5FD7FF" onChange={() => {}} />);
  const radios = screen.getAllByRole("radio");
  expect(radios).toHaveLength(ACCENT_PRESETS.length + 2);
  expect(radios[0]).toHaveAccessibleName("Current colour #5fd7ff");
  expect(radios[0]).toHaveAttribute("aria-checked", "true");
  expect(radios.filter((r) => r.getAttribute("aria-checked") === "true")).toHaveLength(1);
});

test("no colour checks None; arrows move and pick", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(<ProjectColorPicker label="Project colour" value="" onChange={onChange} />);
  const none = screen.getByRole("radio", { name: "None" });
  expect(none).toHaveAttribute("aria-checked", "true");
  none.focus();
  await user.keyboard("{ArrowRight}");
  expect(onChange).toHaveBeenLastCalledWith(ACCENT_PRESETS[0].hex);
  await user.click(screen.getByRole("radio", { name: "Teal" }));
  expect(onChange).toHaveBeenLastCalledWith("#14b8a6");
});
