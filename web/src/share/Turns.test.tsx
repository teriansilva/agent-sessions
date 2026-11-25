import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import type { PublicTurn } from "./publicApi";
import { PeerAttribution, ShareTurn } from "./Turns";

// Attribution (#2061): a turn a paired instance caused is labelled with that peer wherever the
// conversation is drawn. This component is the one place the label is built.
const turn = (over: Partial<PublicTurn> = {}): PublicTurn => ({
  turn_id: "t1",
  state: "completed",
  text: "Keep the batch size as it is.",
  reply: null,
  reply_ts: null,
  reply_truncated: false,
  tools: [],
  tools_truncated: false,
  ...over,
});

test("a peer's turn: the badge with the peer's name and the word Peer, not Operator", () => {
  const { container } = render(<ShareTurn turn={turn({ author: { name: "gpu-box", via: "peer" } })} />);
  const label = screen.getByTestId("peer-attribution");
  expect(label).toHaveTextContent("gpu-box·Peer");
  expect(container.querySelector("[data-author]")).toHaveAttribute("data-author", "peer");
  expect(container.querySelector("[data-avatar]")).toHaveAttribute("data-kind", "peer");
  expect(container).not.toHaveTextContent(/operator/i);
  expect(screen.queryByTestId("peer-edited")).toBeNull();
});

test("an edited suggestion says 'edited' to others and 'edited by you' to the owner", () => {
  const author = { name: "gpu-box", via: "peer" as const, edited: true };
  const { unmount } = render(<ShareTurn turn={turn({ author })} />);
  expect(screen.getByTestId("peer-edited")).toHaveTextContent(/^edited$/);
  unmount();
  render(<ShareTurn turn={turn({ author })} owner />);
  expect(screen.getByTestId("peer-edited")).toHaveTextContent("edited by you");
});

test("the peer's name is text, whatever it is called", () => {
  const { container } = render(<PeerAttribution author={{ name: "<img src=x onerror=alert(1)>" }} />);
  expect(container.querySelector("img")).toBeNull();
  expect(container).toHaveTextContent("<img src=x onerror=alert(1)>");
});

test("an operator's and a visitor's turn are labelled as before", () => {
  const { container, rerender } = render(<ShareTurn turn={turn()} />);
  expect(container).toHaveTextContent("Operator");
  expect(screen.queryByTestId("peer-attribution")).toBeNull();
  rerender(<ShareTurn turn={turn({ author: { name: "Jana", via: "public_link" } })} />);
  expect(container).toHaveTextContent("Jana · via public link");
});

test("a line under the person's message (the remote pane's delivery state)", () => {
  render(<ShareTurn turn={turn()} foot={<p data-testid="foot">sent</p>} />);
  expect(screen.getByTestId("foot")).toHaveTextContent("sent");
});
