/** The boot shows REAL reads only (#1531): a read that has not arrived has no line, and appears
 *  when it lands while the boot still runs (Hermes on #1560: a slow /api/config). */
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { BootSequence } from "./BootSequence";

test("a slow config: no Host or Operator line, no placeholder — the boot does not wait", () => {
  render(<BootSequence on host={null} operator={null} version="0.20.0" />);
  expect(screen.getByTestId("boot-sequence")).toHaveTextContent(/uplink/i);
  expect(screen.getByTestId("boot-sequence")).not.toHaveTextContent(/host|operator|—/i);
  expect(screen.getByTestId("boot-sequence")).toHaveTextContent("v0.20.0");
});

test("the config landing during the boot adds its lines", () => {
  const { rerender } = render(<BootSequence on host={null} operator={null} version={null} />);
  rerender(<BootSequence on host="COMMAND-POST" operator="COMMANDER" version="0.20.0" />);
  expect(screen.getByTestId("boot-sequence")).toHaveTextContent("COMMAND-POST");
  expect(screen.getByTestId("boot-sequence")).toHaveTextContent("COMMANDER");
});

test("off renders nothing", () => {
  render(<BootSequence on={false} host="h" operator="o" version="1" />);
  expect(screen.queryByTestId("boot-sequence")).toBeNull();
});
