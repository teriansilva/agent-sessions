/** The call's cards (#1397 P2): only the newest read of THIS conversation may show its cards. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import { CallCards } from "./CallCards";
import { IDLE } from "./callClient";
import { CallContext } from "./callContext";

const defaultCall = { cardDecided: () => undefined };

vi.mock("../../lib/api", async (orig) => {
  const real = await orig<typeof import("../../lib/api")>();
  return {
    ...real,
    api: { ...real.api, askConversation: vi.fn(), approveAction: vi.fn(), rejectAction: vi.fn() },
  };
});

const nudge = (id: string, open = true) => ({
  kind: "nudge", action_id: id, key: id, title: `card ${id}`, text: "Rerun the tests.",
  state: open ? null : "delivered", projection: open ? "actionable" : "settled",
  can_approve: open, can_reject: open, detail: "",
});
const turnWith = (id: string) => ({
  turns: [{ turn_id: id, status: "done", proposals: [nudge(id)] }],
});
const session = (bypassDefault: boolean) => ({
  kind: "session", action_id: "s1", title: "docs", agent: "claude", text: "Refresh the docs.",
  state: "pending", engine: "claude", bypass_default: bypassDefault, bypass: null, session_key: "",
  reason: "", notice: bypassDefault ? "default_changed" : "", can_start: true, can_dismiss: true,
});
const CID = "c".repeat(32);

test("an older read answering late never brings back cards a newer turn replaced (#1640)", async () => {
  const answers: ((v: unknown) => void)[] = [];
  vi.mocked(api.askConversation).mockImplementation(
    () => new Promise((resolve) => answers.push(resolve as (v: unknown) => void)) as never,
  );
  const { rerender } = render(<CallCards cid={"c".repeat(32)} seq={1} />);
  rerender(<CallCards cid={"c".repeat(32)} seq={2} />); // a newer turn: a second read
  expect(answers).toHaveLength(2);
  await act(async () => answers[1](turnWith("newer"))); // the newer read answers first …
  await act(async () => answers[0](turnWith("older"))); // … the older one late
  const cards = screen.getAllByTestId("call-card").map((c) => c.dataset.card);
  expect(cards).toEqual(["newer"]);
});

test("opening the call shows the conversation's cards at once, before any turn (#1640)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue(turnWith("waiting") as never);
  render(<CallCards cid={CID} seq={0} />);
  await waitFor(() =>
    expect(screen.getAllByTestId("call-card").map((c) => c.dataset.card)).toEqual(["waiting"]),
  );
});

const shown = () => screen.queryAllByTestId("call-card").map((c) => c.dataset.card);
const question = (answered = false) => ({
  question_id: "q1", question: "Which project?", options: ["One", "Two"], allow_text: false, answered,
});

test("a newer turn's card takes an older still-pending card off the call screen (#1911)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [
      { turn_id: "t1", status: "done", proposals: [nudge("older-open"), nudge("older-done", false)] },
      { turn_id: "t2", status: "done", proposals: [nudge("newest")] },
    ],
  } as never);
  render(<CallCards cid={CID} seq={1} />);
  await waitFor(() => expect(shown()).toEqual(["newest"]));
});

test("a newer turn's open question takes the older cards off the call screen (#1911)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [
      { turn_id: "t1", status: "done", proposals: [nudge("older-open")] },
      { turn_id: "t2", status: "done", proposals: [], ask: question() },
    ],
  } as never);
  const { container } = render(<CallCards cid={CID} seq={1} />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalled());
  await act(async () => undefined);
  expect(shown()).toEqual([]);
  expect(container).toBeEmptyDOMElement();
});

test("a turn that presents nothing new leaves the open card where it is (#1911)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [
      { turn_id: "t1", status: "done", proposals: [nudge("open")] },
      { turn_id: "t2", status: "done", proposals: [] },
    ],
  } as never);
  render(<CallCards cid={CID} seq={1} />);
  await waitFor(() => expect(shown()).toEqual(["open"]));
});

test("answering HQ's question never brings the card it replaced back (#1911 review)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [
      { turn_id: "t1", status: "done", proposals: [nudge("replaced")] },
      { turn_id: "t2", status: "done", proposals: [], ask: question(true) }, // answered by t3
      { turn_id: "t3", status: "done", proposals: [] },
    ],
  } as never);
  render(<CallCards cid={CID} seq={1} />);
  await waitFor(() => expect(api.askConversation).toHaveBeenCalled());
  await act(async () => undefined);
  expect(shown()).toEqual([]);
});

test("a card a spoken window still names stays in view after a newer card (#1911 review)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [
      { turn_id: "t1", status: "done", proposals: [nudge("armed")] },
      { turn_id: "t2", status: "done", proposals: [nudge("typed-newer")] },
    ],
  } as never);
  const snap = { ...IDLE, phase: "connected" as const, challenge: { cards: ["armed"], expiresAt: Date.now() + 20_000 } };
  render(
    <CallContext.Provider value={{ ...defaultCall, snap } as never}>
      <CallCards cid={CID} seq={1} />
    </CallContext.Provider>,
  );
  await waitFor(() => expect(shown()).toEqual(["armed", "typed-newer"]));
});

test("a refreshed session card with a changed bypass default starts its switch afresh (#1640)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [{ turn_id: "t1", status: "done", proposals: [session(false)] }],
  } as never);
  const { rerender } = render(<CallCards cid={CID} seq={1} />);
  const sw = await screen.findByTestId("ask-session-bypass");
  expect(within(sw).getByRole("switch")).not.toBeChecked();
  vi.mocked(api.askConversation).mockResolvedValue({
    turns: [{ turn_id: "t1", status: "done", proposals: [session(true)] }],
  } as never);
  rerender(<CallCards cid={CID} seq={2} />);
  await waitFor(() =>
    expect(within(screen.getByTestId("ask-session-bypass")).getByRole("switch")).toBeChecked(),
  );
});

test("a refused nudge decision says why on its card (#1640)", async () => {
  vi.mocked(api.askConversation).mockResolvedValue(turnWith("n1") as never);
  vi.mocked(api.approveAction).mockRejectedValue(new Error("The screen moved on."));
  render(<CallCards cid={CID} seq={1} />);
  await userEvent.click(await screen.findByTestId("ask-nudge-send"));
  expect(await screen.findByTestId("ask-nudge-error")).toBeInTheDocument();
});
