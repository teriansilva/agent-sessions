/** The live strip's fences (#1064 Phase 2): a response that lands after a mission switch never
 *  paints on the new mission, a failed refresh keeps the last reading, and polling runs on the
 *  `NOW_POLL_MS` cadence. */
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { MissionNow } from "../../types/api";

import { MissionNowStrip } from "./MissionNowStrip";
import { NOW_POLL_MS } from "./missionNow";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { missionNow: vi.fn() } };
});

const nowMock = vi.mocked(api.missionNow);

const reading = (
  key: string,
  status: MissionNow["sessions"][number]["status"],
): MissionNow => ({
  checked_at: 1,
  sessions: [
    {
      session_key: key,
      status,
      seconds_since_output: status === "unobserved" ? null : 3,
      prompt_class: status === "at_prompt" ? "choice" : null,
      recap_age_s: null,
      recap_older_than_output: false,
    },
  ],
});

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  nowMock.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("a response for the previous mission never paints on the new one", async () => {
  let resolveA!: (v: MissionNow) => void;
  nowMock.mockImplementationOnce(() => new Promise((r) => (resolveA = r)));
  nowMock.mockResolvedValueOnce(reading("claude:bbbb", "quiet"));
  const { rerender } = render(<MissionNowStrip missionId="msn_a" />);
  rerender(<MissionNowStrip missionId="msn_b" />);
  expect(await screen.findByText("claude · bbbb")).toBeTruthy();
  await act(async () => resolveA(reading("claude:aaaa", "producing")));
  expect(screen.queryByText("claude · aaaa")).toBeNull();
  expect(screen.getByText("claude · bbbb")).toBeTruthy();
});

test("a failed refresh keeps the last reading and says so", async () => {
  nowMock.mockResolvedValueOnce(reading("claude:aaaa", "at_prompt"));
  nowMock.mockRejectedValueOnce(new Error("down"));
  render(<MissionNowStrip missionId="msn_a" />);
  expect(await screen.findByText(/waiting at a choice/)).toBeTruthy();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(NOW_POLL_MS);
  });
  expect(screen.getByText(/waiting at a choice/)).toBeTruthy();
  expect(screen.getByText(/refresh failed/)).toBeTruthy();
  expect(nowMock).toHaveBeenCalledTimes(2);
});

test("a first read that fails says the status is unavailable, never 'no session'", async () => {
  nowMock.mockRejectedValueOnce(new Error("down"));
  render(<MissionNowStrip missionId="msn_a" />);
  expect(await screen.findByText("Live status unavailable.")).toBeTruthy();
  expect(screen.queryByText(/No session/)).toBeNull();
});

test("a mission holding no session says so", async () => {
  nowMock.mockResolvedValueOnce({ checked_at: 1, sessions: [] });
  render(<MissionNowStrip missionId="msn_a" />);
  expect(
    await screen.findByText("No session is held by this mission."),
  ).toBeTruthy();
});

test("a held session's report to HQ shows on its line as agent text (#1882)", async () => {
  const r = reading("claude:aaaa", "quiet");
  r.sessions[0].report = {
    id: "r1",
    state: "needs_decision",
    summary: "Both <b>stores</b> pass",
    question: "Which one?",
    options: ["A", "B"],
    at: 1,
  };
  nowMock.mockResolvedValueOnce(r);
  render(<MissionNowStrip missionId="msn_a" />);
  const line = await screen.findByTestId("mission-now-report");
  expect(line.textContent).toContain("Agent says · asks to decide");
  expect(line.textContent).toContain("Both <b>stores</b> pass — Which one?");
  expect(line.querySelector("b")).toBeNull();
});

test("an API member carries an API tag and a link to its conversation; a console row does not (#1900)", async () => {
  nowMock.mockResolvedValueOnce({
    checked_at: 1,
    sessions: [
      {
        session_key: "claude:aaaa",
        status: "producing",
        seconds_since_output: 3,
        prompt_class: null,
        recap_age_s: null,
        recap_older_than_output: false,
      },
      {
        session_key: "claude-api:8c41aaaa",
        status: "at_prompt",
        seconds_since_output: 40,
        prompt_class: "approval",
        recap_age_s: null,
        recap_older_than_output: false,
        api: true,
        pending_count: 3,
      },
    ],
  });
  render(
    <MemoryRouter>
      <MissionNowStrip missionId="msn_a" />
    </MemoryRouter>,
  );
  expect(await screen.findByText("waiting on 3 approvals · last turn 40s ago")).toBeTruthy();
  const rows = screen.getAllByTestId("mission-now-row");
  expect(within(rows[0]).queryByTestId("mission-now-api")).toBeNull();
  expect(within(rows[0]).queryByRole("link")).toBeNull();
  expect(within(rows[1]).getByTestId("mission-now-api").textContent).toBe("API");
  const link = within(rows[1]).getByRole("link", { name: /conversation/i });
  expect(link.getAttribute("href")).toBe("/s/claude-api/8c41aaaa");
  // At a prompt is the neutral dot, as on a console row: the card owns the ask.
  expect(rows[1].getAttribute("data-status")).toBe("at_prompt");
});
