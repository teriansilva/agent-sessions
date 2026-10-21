/** The NIGHTJAR controls (#1531, docs/design.md v2 §6): their semantics, which the gallery's
 *  pictures cannot show. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, test } from "vitest";

import { Disclosure, Field, Seg, Switch } from "./Controls";

test("Field: the label names the control; hint and error describe it; an error marks it invalid", () => {
  render(<Field label="Project name" defaultValue="atlas" hint="Shown on the map." error="That name is taken." />);
  const input = screen.getByRole("textbox", { name: "Project name" });
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAccessibleDescription("Shown on the map. That name is taken.");
  expect(screen.getByRole("alert")).toHaveTextContent("That name is taken.");
  expect(input).toHaveClass("nj-field");
});

test("Field: a select and a textarea, with no error, are valid and labelled", () => {
  render(
    <>
      <Field label="Unit" as="select" defaultValue="b">
        <option value="a">A</option>
        <option value="b">B</option>
      </Field>
      <Field label="Orders" as="textarea" defaultValue="Hold." mono />
    </>,
  );
  const select = screen.getByRole("combobox", { name: "Unit" });
  expect(select).toHaveValue("b");
  expect(select).not.toHaveAttribute("aria-invalid");
  const area = screen.getByRole("textbox", { name: "Orders" });
  expect(area.tagName).toBe("TEXTAREA");
  expect(area).toHaveClass("is-mono");
});

function SegHarness() {
  const [v, setV] = useState<"a" | "b" | "c">("a");
  return (
    <Seg
      label="Scope"
      value={v}
      onChange={setV}
      options={[
        { value: "a", label: "All" },
        { value: "b", label: "Live", disabled: true },
        { value: "c", label: "Idle" },
      ]}
    />
  );
}

test("Seg: one tab stop; arrow keys move and select, skipping a disabled segment", async () => {
  render(<SegHarness />);
  const all = screen.getByRole("radio", { name: "All" });
  expect(all).toHaveAttribute("aria-checked", "true");
  expect(all).toHaveAttribute("tabindex", "0");
  expect(screen.getByRole("radio", { name: "Idle" })).toHaveAttribute("tabindex", "-1");
  all.focus();
  await userEvent.keyboard("{ArrowRight}");
  const idle = screen.getByRole("radio", { name: "Idle" });
  expect(idle).toHaveAttribute("aria-checked", "true");
  expect(idle).toHaveFocus();
});

test("Disclosure: closed content stays mounted but inert; the toggle reports its state", async () => {
  render(
    <Disclosure summary="Evidence">
      <button type="button">inside</button>
    </Disclosure>,
  );
  const toggle = screen.getByRole("button", { name: /Evidence/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  const body = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(body).toHaveAttribute("inert");
  expect(body.querySelector("button")).not.toBeNull(); // mounted
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(body).not.toHaveAttribute("inert");
});

test("Switch: a role=switch that reports and flips its state", async () => {
  function H() {
    const [on, setOn] = useState(false);
    return <Switch checked={on} onChange={setOn} label="Auto-sort" />;
  }
  render(<H />);
  const sw = screen.getByRole("switch", { name: "Auto-sort" });
  expect(sw).toHaveAttribute("aria-checked", "false");
  await userEvent.click(sw);
  expect(sw).toHaveAttribute("aria-checked", "true");
});
