/** The live strip's fences (#1064 Phase 2): a response that lands after a mission switch never
 *  paints on the new mission, a failed refresh keeps the last reading, and polling runs on the
 *  `NOW_POLL_MS` cadence. */
import { act, cleanup, render, screen } from "@testing-library/react";
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
