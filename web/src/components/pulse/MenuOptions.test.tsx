/** Answering a session's menu from the card (#1060 Phase 3), asserted on the REQUEST.
 *
 *  - One button per server-parsed option, labelled with the agent's words, and nothing without a menu.
 *  - The first tap arms, the second sends — the number AND the label shown, which the server
 *    re-checks against the live screen.
 *  - A refusal is shown verbatim as "Not sent — …"; a may-have-landed answer never says "Not sent".
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";
import type { OrchestratorAction } from "../../types/api";

import { MenuOptions } from "./MenuOptions";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { chooseAction: vi.fn() } };
});

const esc = (menu: unknown = MENU): OrchestratorAction =>
  ({
    id: "esc-1",
    verb: "escalate",
    state: "escalated",
    session_id: "claude:x",
    confidence: 0.9,
    observed_prompt: { prompt_class: "choice", menu, observed_at: 1 },
  }) as unknown as OrchestratorAction;

const MENU = {
  engine: "claude",
  question: "Which colour do you prefer?",
  options: [
    { n: 1, label: "Red", selected: true },
    { n: 2, label: "Green", selected: false },
  ],
};

// Braces, not an expression body: vitest runs a function RETURNED from beforeEach as a teardown, and
// `mockReset()` returns the mock — which then got called after every test.
beforeEach(() => {
  vi.mocked(api.chooseAction).mockReset();
});

test("no menu, no buttons", () => {
  const { container } = render(<MenuOptions action={esc(null)} />);
  expect(container).toBeEmptyDOMElement();
});

test("the first tap arms, the second sends the number and the label shown", async () => {
  vi.mocked(api.chooseAction).mockResolvedValue({
    id: "esc-1",
    state: "rejected",
  } as never);
  const onResolved = vi.fn();
  render(<MenuOptions action={esc()} onResolved={onResolved} />);
  expect(screen.getByText(/Which colour do you prefer\?/)).toBeInTheDocument();
  const [, green] = screen.getAllByTestId("menu-option");
  expect(green).toHaveTextContent("2. Green");
  await userEvent.click(green);
  expect(api.chooseAction).not.toHaveBeenCalled();
  expect(green).toHaveTextContent("Send 2 · Green");
  expect(screen.getByTestId("menu-armed")).toHaveTextContent(
    "Types 2 into the session",
  );
  await userEvent.click(green);
  await waitFor(() =>
    expect(api.chooseAction).toHaveBeenCalledWith("esc-1", 2, "Green"),
  );
  expect(onResolved).toHaveBeenCalledWith({ id: "esc-1", state: "rejected" });
});

test("tapping another option moves the arm instead of sending", async () => {
  render(<MenuOptions action={esc()} />);
  const [red, green] = screen.getAllByTestId("menu-option");
  await userEvent.click(red);
  await userEvent.click(green);
  expect(api.chooseAction).not.toHaveBeenCalled();
  expect(red).toHaveTextContent("1. Red");
  expect(green).toHaveTextContent("Send 2 · Green");
});

test("a refusal is shown verbatim, as not sent", async () => {
  vi.mocked(api.chooseAction).mockRejectedValue(
    new ApiError(
      409,
      "nothing was sent: the session is no longer at that menu",
      {
        detail: "x",
      },
    ),
  );
  const onNote = vi.fn();
  render(<MenuOptions action={esc()} onNote={onNote} />);
  const [red] = screen.getAllByTestId("menu-option");
  await userEvent.click(red);
  await userEvent.click(red);
  expect(await screen.findByTestId("menu-note")).toHaveTextContent(
    "Not sent — nothing was sent: the session is no longer at that menu",
  );
  expect(onNote).toHaveBeenCalled();
  expect(red).toHaveTextContent("1. Red");
});

test("a may-have-landed answer never says 'Not sent'", async () => {
  vi.mocked(api.chooseAction).mockRejectedValue(
    new ApiError(
      502,
      "the choice may or may not have landed (OSError); check the session",
      {
        state: "indeterminate",
      },
    ),
  );
  render(<MenuOptions action={esc()} />);
  const [red] = screen.getAllByTestId("menu-option");
  await userEvent.click(red);
  await userEvent.click(red);
  const note = await screen.findByTestId("menu-note");
  expect(note).toHaveTextContent("may or may not have landed");
  expect(note).not.toHaveTextContent("Not sent");
});
