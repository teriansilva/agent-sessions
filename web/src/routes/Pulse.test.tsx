import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type {
  AppConfig,
  PulseCard,
  PulseConfig,
  PulseOverview,
  PulseState,
} from "../types/api";
import Pulse from "./Pulse";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      pulse: vi.fn(),
      pulseScan: vi.fn(),
      pulseAsk: vi.fn(),
      setPrefs: vi.fn(),
      // #726: the page mounts the orchestrator strip, which reads this on mount.
      orchestrator: vi.fn(),
      orchestrate: vi.fn(),
      evidence: vi.fn(),
    },
  };
});

function card(
  over: Partial<PulseCard> & { id: string; state: PulseState },
): PulseCard {
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
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    pulse,
  } as AppConfig;
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
  vi.mocked(api.orchestrator).mockReset().mockRejectedValue(new Error("off"));
});

test("renders the empty state with the window when nothing was scanned (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({ generated_at: null, cards: [], window_days: 3 }),
  );
  renderPulse();
  expect(
    await screen.findByText(/no work in the last 3 days/i),
  ).toBeInTheDocument();
  expect(screen.getByText(/not scanned yet/i)).toBeInTheDocument();
});

test("renders the banner + one ordered card list with jump links (#441 P5, #754)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      banner: "1 session needs you; one build is in flight.",
      cards: [
        card({
          id: "codex:c-need",
          engine: "codex",
          title: "Deploy step",
          state: "needs_you",
          intervention_required: true,
          intervention_reason: "Confirm the push",
        }),
        card({
          id: "claude:c-live",
          title: "Failing build",
          state: "in_flight",
          live: true,
        }),
        card({
          id: "gemini:c-idle",
          engine: "gemini",
          title: "Tag issues",
          state: "idle",
        }),
      ],
    }),
  );
  renderPulse();
  // Banner (model text rendered as plain text).
  expect(await screen.findByText(/1 session needs you/i)).toBeInTheDocument();
  // ONE list since #754 — the four state sections each left a partial row in the grid. The
  // band moved onto the card's LED, which is now what names it for a screen reader.
  expect(screen.queryByRole("heading", { name: /^needs you$/i })).toBeNull();
  expect(screen.getByRole("img", { name: /needs you/i })).toBeInTheDocument();
  expect(screen.getByRole("img", { name: /idle/i })).toBeInTheDocument();
  // …and the order the headings conveyed is now sort order.
  const titles = screen
    .getAllByRole("listitem")
    .map((li) => li.textContent ?? "");
  const at = (t: string) => titles.findIndex((x) => x.includes(t));
  expect(at("Deploy step")).toBeLessThan(at("Failing build"));
  expect(at("Failing build")).toBeLessThan(at("Tag issues"));
  // Jump links point at /s/:engine/:uuid (engine prefix stripped from the card id).
  const jump = screen.getByRole("link", { name: /jump into deploy step/i });
  expect(jump).toHaveAttribute("href", "/s/codex/c-need");
  expect(
    screen.getByRole("link", { name: /jump into failing build/i }),
  ).toHaveAttribute("href", "/s/claude/c-live");
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
  expect(
    await screen.findByText(/next: rerun the migration/i),
  ).toBeInTheDocument();
  expect(screen.queryByText(/old summary/i)).not.toBeInTheDocument();
});

test("Scan now renders the 409 'already running' against the cached overview (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({ cards: [card({ id: "claude:c1", state: "idle" })] }),
  );
  vi.mocked(api.pulseScan).mockRejectedValue(
    new ApiError(409, "a Pulse scan is already running"),
  );
  renderPulse();
  await screen.findByRole("img", { name: /idle/i });
  await userEvent.click(
    screen.getAllByRole("button", { name: /scan now/i })[0],
  );
  expect(
    await screen.findByText(/a scan is already running/i),
  ).toBeInTheDocument();
  // The cached overview is still shown (not replaced by an error screen).
  expect(screen.getByRole("img", { name: /idle/i })).toBeInTheDocument();
});

test("changing the depth persists the pref and scans at that depth (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({ cards: [card({ id: "claude:c1", state: "idle" })] }),
  );
  vi.mocked(api.pulseScan).mockResolvedValue(overview({ scan_depth: "slow" }));
  renderPulse({
    auto_enabled: false,
    interval_minutes: 30,
    window_days: 3,
    scan_depth: "fast",
    configured: true,
  });
  await screen.findByRole("img", { name: /idle/i });
  await userEvent.click(screen.getByRole("button", { name: "SLOW" }));
  expect(api.setPrefs).toHaveBeenCalledWith({ pulse: { scan_depth: "slow" } });
  await userEvent.click(
    screen.getAllByRole("button", { name: /scan now/i })[0],
  );
  expect(api.pulseScan).toHaveBeenCalledWith({ depth: "slow" });
});

test("a degraded scan (synthesis skipped) tells the user to configure the endpoint (#441 P5)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({ generated_at: null, cards: [] }),
  );
  vi.mocked(api.pulseScan).mockResolvedValue(
    overview({ synthesis_skipped: true, cards: [] }),
  );
  renderPulse();
  await screen.findByText(/no work in the last/i);
  await userEvent.click(
    screen.getAllByRole("button", { name: /scan now/i })[0],
  );
  expect(
    await screen.findByText(/synthesis needs the ai endpoint/i),
  ).toBeInTheDocument();
});

// ---- Ask panel (#522) ----------------------------------------------------------

const ASK_CFG = {
  auto_enabled: false,
  interval_minutes: 30,
  window_days: 3,
  scan_depth: "fast",
  configured: true,
} as PulseConfig;

function askMatch(over: Partial<PulseCard> & { id: string }, why: string) {
  return { ...card({ state: "idle", ...over }), why };
}

test("Ask: unconfigured endpoint disables the input, shows the Settings hint, makes no call (#522)", async () => {
  renderPulse({ ...ASK_CFG, configured: false });
  const input = await screen.findByRole("textbox", {
    name: /ask about your past work/i,
  });
  expect(input).toBeDisabled();
  expect(screen.getByRole("button", { name: /^ask$/i })).toBeDisabled();
  expect(
    screen.getByRole("link", { name: /settings → ai review/i }),
  ).toBeInTheDocument();
  expect(api.pulseAsk).not.toHaveBeenCalled();
});

test("Ask: a question renders the answer plus matched cards with why + Jump in (#522)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "That was your ws delta-resume session.",
    matches: [
      askMatch(
        { id: "claude:ws1", title: "fix ws delta-resume" },
        "transcript discusses reconnect backoff",
      ),
    ],
    stage: "content",
    configured: true,
  });
  renderPulse(ASK_CFG);
  const input = await screen.findByRole("textbox", {
    name: /ask about your past work/i,
  });
  await userEvent.type(input, "which session had the websocket reconnect bug?");
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
  expect(api.pulseAsk).toHaveBeenCalledWith(
    "which session had the websocket reconnect bug?",
    [],
  );
  // The question echoes into the thread; the answer + matched card render below it.
  expect(
    await screen.findByText("That was your ws delta-resume session."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("which session had the websocket reconnect bug?"),
  ).toBeInTheDocument();
  expect(screen.getByText("fix ws delta-resume")).toBeInTheDocument();
  expect(
    screen.getByText(/transcript discusses reconnect backoff/),
  ).toBeInTheDocument();
  // The match reuses the Pulse card — Jump in routes to the session view.
  const jump = screen.getByRole("link", {
    name: /jump into fix ws delta-resume/i,
  });
  expect(jump).toHaveAttribute("href", "/s/claude/ws1");
});

test("Ask: no matches renders the answer line only (#522)", async () => {
  vi.mocked(api.pulseAsk).mockResolvedValue({
    answer: "Nothing in your sessions matches that.",
    matches: [],
    stage: "catalog",
    configured: true,
  });
  renderPulse(ASK_CFG);
  const input = await screen.findByRole("textbox", {
    name: /ask about your past work/i,
  });
  await userEvent.type(input, "did I ever port this to zig?");
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
  expect(
    await screen.findByText("Nothing in your sessions matches that."),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: /jump into/i }),
  ).not.toBeInTheDocument();
});

test("Ask: a busy 409 surfaces the server detail as a note, not an error (#522)", async () => {
  vi.mocked(api.pulseAsk).mockRejectedValue(
    new ApiError(409, "a question is already running"),
  );
  renderPulse(ASK_CFG);
  const input = await screen.findByRole("textbox", {
    name: /ask about your past work/i,
  });
  await userEvent.type(input, "which one?");
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
  expect(
    await screen.findByText(/a question is already running/i),
  ).toBeInTheDocument();
});

test("Ask: a follow-up replays the prior turns as history (#522)", async () => {
  vi.mocked(api.pulseAsk)
    .mockResolvedValueOnce({
      answer: "First answer.",
      matches: [],
      stage: "catalog",
      configured: true,
    })
    .mockResolvedValueOnce({
      answer: "Second answer.",
      matches: [],
      stage: "catalog",
      configured: true,
    });
  renderPulse(ASK_CFG);
  const input = await screen.findByRole("textbox", {
    name: /ask about your past work/i,
  });
  await userEvent.type(input, "first question");
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
  await screen.findByText("First answer.");
  await userEvent.type(input, "and a follow-up");
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
  await screen.findByText("Second answer.");
  expect(api.pulseAsk).toHaveBeenLastCalledWith("and a follow-up", [
    { role: "user", content: "first question" },
    { role: "assistant", content: "First answer." },
  ]);
});

test("a card shows what the orchestrator last did here, when nothing is pending (#777)", async () => {
  // The Activity block was a second list of near-identical boxes above the cards. The session's
  // last action rides its own card now.
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          title: "Docs pass",
          state: "idle",
          last_action: {
            id: "a1",
            state: "expired",
            ts: Math.floor(Date.now() / 1000) - 3600,
            tier: "yolo",
            session_id: "claude:c1",
            engine: "claude",
            title: "Docs pass",
            project: "infra",
            project_id: "p1",
            verb: "escalate",
            confidence: 0.9,
            rationale: "needs a call",
            evidence: "none",
            repeats: 7,
          },
        }),
      ],
    }),
  );
  renderPulse();
  expect(await screen.findByText("ESCALATE")).toBeInTheDocument();
  expect(screen.getByText("expired")).toBeInTheDocument();
  expect(screen.getByText("×7")).toBeInTheDocument();
});

test("a card with a live action shows its controls, not a history line (#777)", async () => {
  vi.mocked(api.pulse).mockResolvedValue(
    overview({
      cards: [
        card({
          id: "claude:c1",
          title: "Docs pass",
          state: "needs_you",
          pending_action: {
            id: "a1",
            state: "proposed",
            ts: Math.floor(Date.now() / 1000),
            tier: "suggest",
            session_id: "claude:c1",
            engine: "claude",
            title: "Docs pass",
            project: "infra",
            project_id: "p1",
            verb: "continue",
            confidence: 0.9,
            rationale: "stopped mid-edit",
            evidence: "none",
          },
        }),
      ],
    }),
  );
  renderPulse();
  // The decision control, and no settled summary beside it.
  expect(
    await screen.findByRole("button", { name: /^approve$/i }),
  ).toBeInTheDocument();
  expect(screen.queryByText("expired")).toBeNull();
});
