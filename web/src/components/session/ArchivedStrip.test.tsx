import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { ArchivedStrip } from "./ArchivedStrip";
import type { UnarchiveState } from "./useArchivedSession";

// #2064: ARCHIVED · READ-ONLY, the compose bar's archived state in both panes.

const strip = (state: UnarchiveState, onUnarchive = vi.fn(), pane: "api" | "terminal" = "api") =>
  render(<ArchivedStrip pane={pane} state={state} onUnarchive={onUnarchive} />);

test("idle: the title, the frozen copy and UNARCHIVE, which takes focus when the strip appears", () => {
  strip({ phase: "idle" });
  const region = screen.getByRole("region", { name: "Archived session" });
  expect(region).toHaveTextContent(/archived · read-only/i);
  expect(region).toHaveTextContent(
    "This session is frozen: no processes, no memory. Unarchive it to send a message — the agent resumes on your next send.",
  );
  const btn = screen.getByRole("button", { name: "Unarchive" });
  expect(btn).toBeEnabled();
  expect(btn).toHaveFocus();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("the terminal pane's copy says the agent resumes on the next send", () => {
  strip({ phase: "idle" }, vi.fn(), "terminal");
  expect(screen.getByRole("region", { name: "Archived session" })).toHaveTextContent(
    /Unarchive it to send a message — the agent resumes on your next send/,
  );
});

test("pressing UNARCHIVE (mouse or keyboard) calls onUnarchive", async () => {
  const user = userEvent.setup();
  const onUnarchive = vi.fn();
  strip({ phase: "idle" }, onUnarchive);
  await user.keyboard("{Enter}"); // focus is already on it
  expect(onUnarchive).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole("button", { name: "Unarchive" }));
  expect(onUnarchive).toHaveBeenCalledTimes(2);
});

test("busy: UNARCHIVING…, disabled, and it says the composer comes back once active", async () => {
  const user = userEvent.setup();
  const onUnarchive = vi.fn();
  strip({ phase: "busy" }, onUnarchive);
  const btn = screen.getByRole("button", { name: "Unarchiving…" });
  expect(btn).toBeDisabled();
  expect(btn).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("status")).toHaveTextContent(
    "Unarchiving… the composer comes back once the session reads active.",
  );
  await user.click(btn);
  expect(onUnarchive).not.toHaveBeenCalled();
});

test("failed: the reason in an alert, still archived, and RETRY calls onUnarchive again", async () => {
  const user = userEvent.setup();
  const onUnarchive = vi.fn();
  const view = strip({ phase: "busy" }, onUnarchive);
  // The failure lands on the mounted strip (busy → failed): RETRY gets focus back.
  view.rerender(
    <ArchivedStrip
      pane="api"
      state={{ phase: "failed", message: "this session is held by a mission" }}
      onUnarchive={onUnarchive}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Couldn’t unarchive: this session is held by a mission. Still archived.",
  );
  const retry = screen.getByRole("button", { name: "Retry" });
  expect(retry).toBeEnabled();
  expect(retry).toHaveFocus();
  await user.click(retry);
  expect(onUnarchive).toHaveBeenCalledTimes(1);
});

test("it does not steal the caret from a field the operator is typing in elsewhere", () => {
  render(
    <>
      <textarea aria-label="elsewhere" />
    </>,
  );
  const elsewhere = screen.getByRole("textbox", { name: "elsewhere" });
  elsewhere.focus();
  strip({ phase: "idle" });
  expect(elsewhere).toHaveFocus();
});
