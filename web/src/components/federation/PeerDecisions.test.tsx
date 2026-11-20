import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { cleanDecisions, cleanRequest } from "../../lib/federationTogether";
import { PeerDecisionCard, SettledDecisionCard } from "./PeerDecisions";

// A decision both sides can see (#2062), as components: what a peer's card lets it press, and
// what a settled card says. Real layout, the keyboard in a browser and the phone are the browser
// spec's (e2e/peer-together.spec.ts).

const HOSTILE = '<img src=x onerror="window.__pwned=1"> **bold** [x](https://evil.example)';
const OFFER = "o".repeat(32);
const cmd = (choices: string[], over: Record<string, unknown> = { offer: OFFER }) =>
  cleanRequest({ request_id: "req-7", turn_id: "t1", kind: "command", summary: HOSTILE, choices, ...over })!;

test("a send peer's card: once and reject answer; the widening option has no way to fire", async () => {
  const onAnswer = vi.fn();
  render(<PeerDecisionCard request={cmd(["approve", "always", "reject"])} level="send" owner="studio" state={null} onAnswer={onAnswer} />);
  const card = screen.getByTestId("peer-decision");
  expect(within(card).getByTestId("peer-decision-word").textContent).toBe("you may answer");
  const always = within(card).getByRole("button", { name: /Approve always/ });
  expect(always).toBeDisabled();
  await userEvent.click(always);
  always.click(); // even a synthetic click on the element itself
  expect(onAnswer).not.toHaveBeenCalled();
  expect(within(card).getByTestId("peer-decision-line").textContent).toBe("Only studio's operator can allow this always.");
  await userEvent.click(within(card).getByRole("button", { name: "Approve once" }));
  await userEvent.click(within(card).getByRole("button", { name: "Reject" }));
  expect(onAnswer.mock.calls).toEqual([
    ["approve", null],
    ["reject", null],
  ]);
  // Other-party text is characters.
  expect(within(card).getByTestId("peer-decision-summary").textContent).toBe(HOSTILE);
  expect(card.querySelector("img, a, strong")).toBeNull();
});

test("a view or suggest peer's card has no control at all", () => {
  for (const level of ["view", "suggest"]) {
    const { unmount } = render(
      <PeerDecisionCard request={cmd(["approve", "reject"])} level={level} owner="studio" state={null} onAnswer={vi.fn()} />,
    );
    const card = screen.getByTestId("peer-decision");
    expect(card.querySelectorAll("button")).toHaveLength(0);
    expect(within(card).getByTestId("peer-decision-line").textContent).toBe("Waiting for an answer.");
    unmount();
  }
});

test("choices with no offer, or one of another shape, draw no answer control (fail closed)", () => {
  const shapes = [{}, { offer: null }, { offer: "" }, { offer: "o".repeat(31) }, { offer: "o".repeat(33) }, { offer: `${"o".repeat(31)}!` }, { offer: 12 }];
  for (const over of shapes) {
    const onAnswer = vi.fn();
    const { unmount } = render(
      <PeerDecisionCard request={cmd(["approve", "always", "reject"], over)} level="send" owner="studio" state={null} onAnswer={onAnswer} />,
    );
    const card = screen.getByTestId("peer-decision");
    expect(card).toHaveAttribute("data-role", "watch");
    expect(card.querySelectorAll("button")).toHaveLength(0);
    expect(within(card).getByTestId("peer-decision-word").textContent).toBe("waiting");
    expect(within(card).getByTestId("peer-decision-line").textContent).toBe("Waiting for an answer.");
    expect(onAnswer).not.toHaveBeenCalled();
    unmount();
  }
});

test("a question is answered by index, never by its label", async () => {
  const onAnswer = vi.fn();
  const q = cleanRequest({
    request_id: "req-q",
    turn_id: "t1",
    kind: "AskUserQuestion",
    choices: ["answer", "reject"],
    offer: OFFER,
    questions: [{ question: "Keep it?", multi: false, options: ["<b>Keep</b>", "Remove"] }],
  })!;
  render(<PeerDecisionCard request={q} level="send" owner="studio" state={null} onAnswer={onAnswer} />);
  const send = screen.getByRole("button", { name: "Send answer" });
  expect(send).toBeDisabled(); // nothing picked yet
  await userEvent.click(screen.getByRole("radio", { name: "<b>Keep</b>" }));
  await userEvent.click(send);
  expect(onAnswer).toHaveBeenCalledWith("answer", [[0]]);
});

test("a lost answer removes the buttons and says who answered; nothing is offered again", () => {
  render(
    <PeerDecisionCard
      request={cmd(["approve", "reject"])}
      level="send"
      owner="studio"
      state={{
        attempt: { requestKey: "t1\0req-7", answer: "x", client_key: "k" },
        phase: "done",
        outcome: { kind: "lost", text: "Already answered by studio's operator." },
      }}
      onAnswer={vi.fn()}
    />,
  );
  const card = screen.getByTestId("peer-decision");
  expect(card.querySelectorAll("button")).toHaveLength(0);
  expect(within(card).getByTestId("peer-decision-outcome").textContent).toBe("Already answered by studio's operator.");
});

test("a settled card names the stored author on each side", () => {
  const [d] = cleanDecisions([
    { request_id: "req-7", turn_id: "t1", answer: "once", author: { via: "peer", name: "gpu-box" }, mine: false },
  ]);
  const seen = { kind: "command", summary: "pytest", question: null };
  const { unmount } = render(<SettledDecisionCard decision={d} seen={seen} side="owner" owner="" lost={false} />);
  expect(screen.getByTestId("settled-answer").textContent).toBe("Approved once");
  expect(screen.getByTestId("settled-by").textContent).toBe("answered by gpu-box");
  unmount();
  render(<SettledDecisionCard decision={{ ...d, author: { via: "owner", name: "" } }} seen={seen} side="peer" owner="studio" lost />);
  expect(screen.getByTestId("settled-by").textContent).toBe("answered by studio's operator");
  expect(screen.getByTestId("settled-lost").textContent).toBe("Already answered by studio's operator.");
});

test.each([1e300, Number.NaN, Number.POSITIVE_INFINITY, -1, "x"])(
  "a settled card built from a decision list carrying %s is drawn, names its author, and does not throw",
  (v) => {
    const [d] = cleanDecisions([
      { request_id: "req-9", turn_id: v, answer: "once", author: { via: "peer", name: "gpu-box", at: v }, mine: v, decided_at: v },
    ]);
    render(<SettledDecisionCard decision={d} seen={undefined} side="owner" owner="" lost={false} />);
    expect(screen.getByTestId("settled-by").textContent).toBe("answered by gpu-box");
    expect(screen.getByTestId("settled-answer").textContent).toBe("Approved once");
  },
);
