/** Settings → Appearance → Subagent sessions (#2325): the device-local display mode the sidebar
 *  reads. Default `nested`; the stored key is the one the sidebar toggle used, so a choice carries
 *  over. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test } from "vitest";
import { SubagentModeSetting } from "./SubagentModeSetting";
import { readSubagentMode, SUBAGENT_MODE_KEY } from "./subagentMode";

beforeEach(() => localStorage.clear());

test("defaults to nested", () => {
  render(<SubagentModeSetting />);
  const group = screen.getByRole("radiogroup", { name: "Subagent sessions" });
  expect(group).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /Nested/ })).toHaveAttribute("aria-checked", "true");
  expect(screen.getByRole("radio", { name: /Hidden/ })).toHaveAttribute("aria-checked", "false");
});

test("keeps an existing choice and persists a new one on this device", async () => {
  localStorage.setItem(SUBAGENT_MODE_KEY, "hidden");
  const user = userEvent.setup();
  render(<SubagentModeSetting />);
  expect(screen.getByRole("radio", { name: /Hidden/ })).toHaveAttribute("aria-checked", "true");
  await user.click(screen.getByRole("radio", { name: /Nested/ }));
  expect(localStorage.getItem(SUBAGENT_MODE_KEY)).toBe("nested");
  expect(readSubagentMode()).toBe("nested");
  expect(screen.getByRole("radio", { name: /Nested/ })).toHaveAttribute("aria-checked", "true");
});
