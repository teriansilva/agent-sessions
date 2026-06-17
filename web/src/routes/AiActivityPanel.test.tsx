import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { AiActivity } from "../types/api";
import { AiActivityPanel } from "./AiActivityPanel";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { aiActivity: vi.fn() } };
});

// Fake timers (frozen) so the panel's 1s ticker / 3s poll never fire spontaneously mid-assert
// (an unwrapped setState → act warning). The mount poll() is a resolved promise we flush by hand.
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(api.aiActivity).mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

async function flush() {
  // Let the mount effect's aiActivity() promise + its .then(setState) settle, inside act.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

test("lists the known AI kinds, idle until they run (#441 P6)", async () => {
  vi.mocked(api.aiActivity).mockResolvedValue({ running: [], last: {} });
  render(<AiActivityPanel />);
  await flush();
  expect(screen.getByText("Pulse scan")).toBeInTheDocument();
  expect(screen.getByText("AI review")).toBeInTheDocument();
  expect(screen.getByText("Auto-sort")).toBeInTheDocument();
  expect(screen.getAllByText("idle").length).toBeGreaterThanOrEqual(3);
});

test("shows a running scan + a last-run summary (#441 P6)", async () => {
  const now = Math.floor(Date.now() / 1000);
  const activity: AiActivity = {
    running: [{ kind: "pulse-scan", detail: "manual", started_at: now - 6 }],
    last: { "ai-review": { finished_at: now - 120, ok: true, detail: "sweep", duration_s: 3.2 } },
  };
  vi.mocked(api.aiActivity).mockResolvedValue(activity);
  render(<AiActivityPanel />);
  await flush();
  // The running pulse scan reports its elapsed time + detail; ai-review shows its last run.
  expect(screen.getByText(/running .* manual/i)).toBeInTheDocument();
  expect(screen.getByText(/ran .*ago/i)).toBeInTheDocument();
});
