/** An agent's tool-permission dialog, answered from its card (#1213), asserted on the REQUEST.
 *
 *  - The dialog's own words: who asks, which tool, the command in full, one button per option.
 *  - Arm, then send — the option number and the label shown; the server builds the keys.
 *  - A persistent grant says so when armed, before it can be sent.
 *  - Sent: the card stops offering buttons and says what was sent. May-have-landed: no retry.
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

const PROMPT = {
  engine: "opencode",
  kind: "permission",
  parser: "opencode-permission",
  heading: "Permission required",
  title: "# Shell command",
  detail: "$ git log --oneline -15 && git log --all -i --grep=mission --oneline",
  question: "Allow this?",
  options: [
    { n: 1, label: "Allow once", selected: true, persistent: false },
    { n: 2, label: "Allow always", selected: false, persistent: true },
    { n: 3, label: "Reject", selected: false, persistent: false },
  ],
};

const esc = (permission: unknown = PROMPT): OrchestratorAction =>
  ({
    id: "esc-p",
    verb: "escalate",
    state: "escalated",
    session_id: "opencode:ses_x",
    confidence: 1,
    escalation_reason: "permission",
    observed_prompt: { prompt_class: "confirm", menu: null, permission, observed_at: 1 },
  }) as unknown as OrchestratorAction;

beforeEach(() => {
  vi.mocked(api.chooseAction).mockReset();
});

test("the card shows the dialog itself", () => {
  render(<MenuOptions action={esc()} />);
  expect(screen.getByTestId("permission-card")).toHaveTextContent(
    "opencode asks permission",
  );
  expect(screen.getByText("# Shell command")).toBeInTheDocument();
  expect(screen.getByTestId("permission-detail")).toHaveTextContent(
    "$ git log --oneline -15 && git log --all -i --grep=mission --oneline",
  );
  expect(screen.getAllByTestId("permission-option").map((b) => b.textContent)).toEqual([
    "Allow once",
    "Allow always",
    "Reject",
  ]);
  expect(screen.queryByTestId("menu-options")).toBeNull();
});

test("arm, then send the number and the label", async () => {
  vi.mocked(api.chooseAction).mockResolvedValue({ id: "esc-p", state: "rejected" } as never);
  const onResolved = vi.fn();
  render(<MenuOptions action={esc()} onResolved={onResolved} />);
  const reject = screen.getAllByTestId("permission-option")[2];
  await userEvent.click(reject);
  expect(api.chooseAction).not.toHaveBeenCalled();
  expect(reject).toHaveTextContent("Send · Reject");
  expect(screen.queryByTestId("permission-warning")).toBeNull();
  await userEvent.click(reject);
  await waitFor(() => expect(api.chooseAction).toHaveBeenCalledWith("esc-p", 3, "Reject"));
  expect(onResolved).toHaveBeenCalled();
  expect(screen.getByTestId("permission-done")).toHaveTextContent("You chose Reject · sent");
  expect(screen.queryAllByTestId("permission-option")).toHaveLength(0);
});

test("a persistent grant warns before it can be sent", async () => {
  render(<MenuOptions action={esc()} />);
  const always = screen.getAllByTestId("permission-option")[1];
  expect(always).toHaveAttribute("data-persistent", "true");
  await userEvent.click(always);
  expect(screen.getByTestId("permission-warning")).toHaveTextContent("Persistent.");
});

test("a refusal says nothing was sent and keeps the buttons", async () => {
  vi.mocked(api.chooseAction).mockRejectedValue(
    new ApiError(
      409,
      "nothing was sent: the session is showing a different permission prompt now",
      { detail: "nothing was sent: the session is showing a different permission prompt now" },
    ),
  );
  const onNote = vi.fn();
  render(<MenuOptions action={esc()} onNote={onNote} />);
  const once = screen.getAllByTestId("permission-option")[0];
  await userEvent.click(once);
  await userEvent.click(once);
  await waitFor(() =>
    expect(screen.getByTestId("permission-note")).toHaveTextContent(
      "Not sent — nothing was sent: the session is showing a different permission prompt now",
    ),
  );
  expect(onNote).toHaveBeenCalled();
  expect(screen.getAllByTestId("permission-option")).toHaveLength(3);
});

test("an answer that may have landed offers no retry", async () => {
  vi.mocked(api.chooseAction).mockRejectedValue(
    new ApiError(502, "the choice may or may not have landed", {
      state: "indeterminate",
    }),
  );
  render(<MenuOptions action={esc()} />);
  const once = screen.getAllByTestId("permission-option")[0];
  await userEvent.click(once);
  await userEvent.click(once);
  await waitFor(() =>
    expect(screen.getByTestId("permission-note")).toHaveTextContent(
      "may or may not have reached the session",
    ),
  );
  expect(screen.getByTestId("permission-note")).not.toHaveTextContent("Not sent");
  expect(screen.queryAllByTestId("permission-option")).toHaveLength(0);
});

test("a gateway error or a dropped connection is treated as may-have-landed", async () => {
  for (const err of [new ApiError(504, "POST /api/pulse/actions/esc-p/choose → 504"), new TypeError("Failed to fetch")]) {
    vi.mocked(api.chooseAction).mockReset();
    vi.mocked(api.chooseAction).mockRejectedValue(err);
    const { unmount } = render(<MenuOptions action={esc()} />);
    const once = screen.getAllByTestId("permission-option")[0];
    await userEvent.click(once);
    await userEvent.click(once);
    await waitFor(() =>
      expect(screen.getByTestId("permission-note")).toHaveTextContent(
        "may or may not have reached the session",
      ),
    );
    expect(screen.queryAllByTestId("permission-option")).toHaveLength(0);
    unmount();
  }
});
