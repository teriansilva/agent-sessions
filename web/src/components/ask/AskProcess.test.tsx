/** The process row (#2076): one <details> that is collapsed by default, opens to the steps, and
 *  says what a stopped turn may say. */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";

import type { AskToolStep } from "../../types/api";
import { AskProcess } from "./AskProcess";
import { processFromServer } from "./askProcess";

const steps: AskToolStep[] = [
  { name: "find_sessions", outcome: "ok", n: 1, ms: 800, count: 12, args: { query: "#1613" } },
  { name: "session_status", outcome: "refused", n: 2, ms: 100, error: "unknown_key", args: { key: "claude:4be1" } },
];

test("running: collapsed, the step in words, the server's count; opened, the live step is marked", async () => {
  const process = processFromServer(
    { tools: steps, steps: { count: 2, more: null, incomplete: false, live: { n: 3, tool: "session_status", at: 1 } }, elapsed_ms: 42_000 },
    Date.now(),
  );
  render(<AskProcess process={process} running step={{ step: "tool", tool: "session_status" }} />);
  const row = screen.getByTestId("ask-working");
  expect(row.tagName).toBe("DETAILS");
  expect(row).not.toHaveAttribute("open");
  expect(screen.getByRole("status")).toHaveTextContent("Reading a session's status…");
  expect(screen.getByTestId("ask-process-tally")).toHaveTextContent(/^2 steps · 42 s$/);
  await userEvent.click(screen.getByTestId("ask-process-summary"));
  expect(row).toHaveAttribute("open");
  const lines = screen.getAllByTestId("ask-process-step");
  expect(lines).toHaveLength(2);
  expect(lines[0]).toHaveTextContent("01");
  expect(lines[0]).toHaveTextContent("Searching your sessions and missions");
  expect(lines[0]).toHaveTextContent("“#1613”");
  expect(lines[0]).toHaveTextContent("12 matches");
  expect(lines[0]).toHaveTextContent("0.8 s");
  expect(within(lines[1]).getByText("refused: unknown key")).toHaveAttribute("data-tone", "refused");
  const live = screen.getByTestId("ask-process-live");
  expect(live).toHaveTextContent("03");
  expect(live).toHaveTextContent("Running");
  expect(live).toHaveAttribute("aria-current", "step");
});

test("while the model thinks between tools the summary says so and no list line is live", () => {
  const process = processFromServer({ tools: steps, steps: { count: 2, more: null, incomplete: false } }, 0);
  render(<AskProcess process={process} running step={{ step: "thinking", round: 2 }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Putting the answer together…");
  expect(screen.queryByTestId("ask-process-live")).toBeNull();
});

test("settled: `N steps · time`, collapsed, no bar, and it opens to the same list", async () => {
  const process = processFromServer(
    { tools: steps, steps: { count: 2, more: null, incomplete: false }, elapsed_ms: 108_000 },
    0,
  );
  const { container } = render(<AskProcess process={process} running={false} step={null} />);
  const row = screen.getByTestId("ask-process");
  expect(row).not.toHaveAttribute("open");
  expect(row).toHaveAttribute("data-state", "settled");
  expect(screen.getByTestId("ask-process-tally")).toHaveTextContent(/^2 steps · 1 m 48 s$/);
  expect(container.querySelector(".nj-acquire")).toBeNull();
  expect(screen.queryByTestId("ask-stopped-line")).toBeNull();
  await userEvent.click(screen.getByTestId("ask-process-summary"));
  expect(screen.getAllByTestId("ask-process-step")).toHaveLength(2);
});

test("stopped: the row is open, says `Stopped after N steps`, and the quiet line follows", () => {
  const process = processFromServer(
    { tools: steps, steps: { count: 2, more: null, incomplete: false }, elapsed_ms: 58_000 },
    0,
  );
  render(<AskProcess process={process} running={false} stopped step={null} />);
  const row = screen.getByTestId("ask-process");
  expect(row).toHaveAttribute("open");
  expect(row).toHaveAttribute("data-state", "stopped");
  expect(screen.getByTestId("ask-process-tally")).toHaveTextContent("Stopped after 2 steps");
  expect(screen.getByTestId("ask-process-summary")).toHaveTextContent("58 s");
  expect(screen.getByTestId("ask-stopped-line")).toHaveTextContent("Stopped by you. Nothing was sent.");
});

test("a stopped turn with a send of unknown outcome never says nothing was sent", () => {
  const sent: AskToolStep = { name: "send_nudge", outcome: "ok", n: 3, ms: 2_400, sent: "unknown", effect: true };
  const process = processFromServer({ tools: [...steps, sent] }, 0);
  render(<AskProcess process={process} running={false} stopped step={null} />);
  expect(screen.getByTestId("ask-stopped-line")).toHaveTextContent(
    "Stopped by you. A message may have been sent — see the steps.",
  );
  expect(screen.getByText("outcome unknown")).toHaveAttribute("data-tone", "unknown");
});

test("the steps past the stored ones are one closing line; an incomplete count has none", () => {
  const more = { count: 1_240, ms: 0, tools: { session_status: 1_240 }, refused: {} };
  const full = processFromServer({ tools: steps, steps: { count: 1_242, more, incomplete: false } }, 0);
  const { unmount } = render(<AskProcess process={full} running={false} step={null} />);
  expect(screen.getByTestId("ask-process-tally")).toHaveTextContent("1,242 steps");
  expect(screen.getByTestId("ask-process-more")).toHaveTextContent("… and 1,240 more lookups");
  expect(screen.getByTestId("ask-process-more")).toHaveTextContent("Reading a session's status × 1,240");
  unmount();
  const cut = processFromServer({ tools: steps, steps: { count: 2, more: null, incomplete: true } }, 0);
  render(<AskProcess process={cut} running={false} step={null} />);
  expect(screen.getByTestId("ask-process-tally")).toHaveTextContent("2+ steps, count incomplete");
  expect(screen.queryByTestId("ask-process-more")).toBeNull();
});
