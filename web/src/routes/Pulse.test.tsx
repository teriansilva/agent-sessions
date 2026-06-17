import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AppConfig, PulseCard, PulseConfig, PulseOverview, PulseState } from "../types/api";
import Pulse from "./Pulse";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { pulse: vi.fn(), pulseScan: vi.fn(), setPrefs: vi.fn() } };
});

function card(over: Partial<PulseCard> & { id: string; state: PulseState }): PulseCard {
  return {
    engine: "claude",
    title: "A session",
    cwd: "/seed/alpha",
    project: { kind: "folder", id: "/seed/alpha", name: "alpha" },
    last_activity: Math.floor(Date.now() / 1000) - 100,
    ai_summary: "did a thing",
    intervention_required: false,
    intervention_reason: "",
    reviewed_at: null,
    live: false,
    synthesis: null,
    ...over,
  };
}

function overview(over: Partial<PulseOverview> = {}): PulseOverview {
  return {
    cache_version: 1,
    generated_at: Math.floor(Date.now() / 1000) - 60,
    window_days: 3,
    scan_depth: "medium",
    input_fingerprint: "fp",
    synthesis_skipped: false,
    banner: null,
    cards: [],
    ...over,
  };
}

function renderPulse(pulse: PulseConfig | undefined = undefined) {
  const config = { csrf: "t", new_session_engines: [], terminal_backend: "ws", pulse } as AppConfig;
  return render(
    <MemoryRouter>
      <ConfigCtx.Provider value={config}>
        <Pulse />
      </ConfigCtx.Provider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.pulse).mockReset().mockResolvedValue(overview());
  vi.mocked(api.pulseScan).mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
});

test("renders the empty state with the window when nothing was scanned (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(overview({ generated_at: null, cards: [], window_days: 3 }));
  renderPulse();
  expect(await screen.findByText(/no work in the last 3 days/i)).toBeInTheDocument();
  expect(screen.getByText(/not scanned yet/i)).toBeInTheDocument();
});

test("renders the banner + cards grouped by state with jump links (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      banner: "1 session needs you; one build is in flight.",
      cards: [
        card({ id: "codex:c-need", engine: "codex", title: "Deploy step", state: "needs_you", intervention_required: true, intervention_reason: "Confirm the push" }),
        card({ id: "claude:c-live", title: "Failing build", state: "in_flight", live: true }),
        card({ id: "gemini:c-idle", engine: "gemini", title: "Tag issues", state: "idle" }),
      ],
    }),
  );
  renderPulse();
  // Banner (model text rendered as plain text).
  expect(await screen.findByText(/1 session needs you/i)).toBeInTheDocument();
  // Group headings present for the populated buckets.
  expect(screen.getByRole("heading", { name: /needs you/i })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /in flight/i })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /idle/i })).toBeInTheDocument();
  // Jump links point at /s/:engine/:uuid (engine prefix stripped from the card id).
  const jump = screen.getByRole("link", { name: /jump into deploy step/i });
  expect(jump).toHaveAttribute("href", "/s/codex/c-need");
  expect(screen.getByRole("link", { name: /jump into failing build/i })).toHaveAttribute(
    "href",
    "/s/claude/c-live",
  );
  // The intervention reason surfaces on the needs-you card.
  expect(screen.getByText(/confirm the push/i)).toBeInTheDocument();
});

test("shows the per-session synthesis line instead of the summary when present (#441 P4/P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          state: "recently_active",
          ai_summary: "old summary",
          synthesis: "Next: rerun the migration",
        }),
      ],
    }),
  );
  renderPulse();
  expect(await screen.findByText(/next: rerun the migration/i)).toBeInTheDocument();
  expect(screen.queryByText(/old summary/i)).not.toBeInTheDocument();
});

test("Scan now renders the 409 'already running' against the cached overview (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(overview({ cards: [card({ id: "claude:c1", state: "idle" })] }));
  vi.mocked(api.pulseScan).mockRejectedValue(new ApiError(409, "a Pulse scan is already running"));
  renderPulse();
  await screen.findByRole("heading", { name: /idle/i });
  await userEvent.click(screen.getAllByRole("button", { name: /scan now/i })[0]);
  expect(await screen.findByText(/a scan is already running/i)).toBeInTheDocument();
  // The cached overview is still shown (not replaced by an error screen).
  expect(screen.getByRole("heading", { name: /idle/i })).toBeInTheDocument();
});

test("changing the depth persists the pref and scans at that depth (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(overview({ cards: [card({ id: "claude:c1", state: "idle" })] }));
  vi.mocked(api.pulseScan).mockResolvedValue(overview({ scan_depth: "slow" }));
  renderPulse({ auto_enabled: false, interval_minutes: 30, window_days: 3, scan_depth: "fast", configured: true });
  await screen.findByRole("heading", { name: /idle/i });
  await userEvent.click(screen.getByRole("button", { name: "SLOW" }));
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { scan_depth: "slow" } });
  await userEvent.click(screen.getAllByRole("button", { name: /scan now/i })[0]);
  expect(api.pulseScan).toHaveBeenCalledWith({ depth: "slow" });
});

test("a degraded scan (synthesis skipped) tells the user to configure the endpoint (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(overview({ generated_at: null, cards: [] }));
  vi.mocked(api.pulseScan).mockResolvedValue(overview({ synthesis_skipped: true, cards: [] }));
  renderPulse();
  await screen.findByText(/no work in the last/i);
  await userEvent.click(screen.getAllByRole("button", { name: /scan now/i })[0]);
  expect(await screen.findByText(/synthesis needs the ai endpoint/i)).toBeInTheDocument();
});
