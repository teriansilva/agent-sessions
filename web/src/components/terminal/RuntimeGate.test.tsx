import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { getRoster, resetRoster, setRoster } from "../../app/engineRoster";
import { RuntimeGate } from "./RuntimeGate";

// #853 §7 / P4: the session surface is chosen by the engine's runtime, never assumed.

test("a pty engine gets its terminal", () => {
  render(<RuntimeGate engine="claude">TERMINAL</RuntimeGate>);
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});

test("an engine of another runtime gets the explicit panel, never a terminal", () => {
  setRoster(getRoster().engines.map((e) => (e.id === "claude" ? { ...e, runtime: "chat" } : e)));
  render(<RuntimeGate engine="claude">TERMINAL</RuntimeGate>);
  expect(screen.queryByText("TERMINAL")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent(/needs a newer BattleLab/);
  expect(screen.getByRole("status")).toHaveTextContent(/Nothing was started/);
});

test("while the roster loads — and for an id it does not list — the terminal renders as before", () => {
  resetRoster();
  const { rerender } = render(<RuntimeGate engine="claude">TERMINAL</RuntimeGate>);
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
  rerender(<RuntimeGate engine="zeta">TERMINAL</RuntimeGate>);
  expect(screen.getByText("TERMINAL")).toBeInTheDocument();
});
