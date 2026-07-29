/** Pulse orchestrator strip (#726 Phase 1).
 *
 * Pinned: the ceiling is SHOWN (the tier alone must never imply more than it grants), a
 * deliverable proposal is labelled "would send" while Phase 1 has no write path, evidence is
 * fetched from the server on expand rather than rendered from anything the model said, the feed
 * groups by project, and a failing orchestrator endpoint degrades to no strip rather than a
 * blank page.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../../lib/api";
import type { OrchestratorAction, OrchestratorConfig } from "../../types/api";
import { Orchestrator } from "./Orchestrator";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { orchestrator: vi.fn(), orchestrate: vi.fn(), evidence: vi.fn(), setPrefs: vi.fn() },
  };
});

function config(over: Partial<OrchestratorConfig> = {}): OrchestratorConfig {
  return {
    enabled: true,
    autonomy: "suggest",
    allowed_verbs: ["continue"],
    auto_verbs_ceiling: ["continue"],
    confidence_min: 0.75,
    interval_minutes: 10,
    max_actions_per_pass: 4,
    proposal_ttl_minutes: 30,
    nudge_template: "carry on",
    prompt: "p",
    notify: "escalations",
    configured: true,
    default_prompt: "p",
    default_nudge_template: "carry on",
    ...over,
  };
}

function action(over: Partial<OrchestratorAction> = {}): OrchestratorAction {
  return {
    id: "a1",
    state: "proposed",
    ts: Date.now() / 1000,
    tier: "suggest",
    session_id: "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    engine: "claude",
    title: "Kimi transcript adapter",
    project: "agent-sessions",
    project_id: "p1",
    verb: "continue",
    confidence: 0.86,
    rationale: "stopped without running the tests it planned",
    evidence: "screen",
    ...over,
  };
}

function renderIt() {
  return render(
    <MemoryRouter>
      <Orchestrator />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.mocked(api.orchestrator).mockReset();
  vi.mocked(api.orchestrate).mockReset();
  vi.mocked(api.evidence).mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
});

test("shows the server-owned autonomy ceiling, not just the tier (#726)", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config({ autonomy: "yolo" }),
    pending: [],
    feed: [],
    expired_now: 0,
  });
  renderIt();
  // YOLO is selected — but the copy must still say what it can actually deliver on its own,
  // because the tier name alone reads as "does everything".
  expect(await screen.findByRole("button", { name: "YOLO" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByText(/acts on its own:/i)).toHaveTextContent("continue");
  expect(screen.getByText(/everything else always waits for you/i)).toBeInTheDocument();
});

test("a deliverable proposal is labelled 'would send' while Phase 1 cannot write", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [action()],
    feed: [action()],
    expired_now: 0,
  });
  renderIt();
  expect(await screen.findByText(/needs a decision · 1/i)).toBeInTheDocument();
  expect(screen.getAllByText(/would send/i).length).toBeGreaterThan(0);
});

test("evidence is fetched from the server on expand, never rendered from the model", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [action()],
    expired_now: 0,
  });
  vi.mocked(api.evidence).mockResolvedValue({
    kind: "screen",
    text: "› waiting for input",
    available: true,
  });
  renderIt();
  const toggle = await screen.findByRole("button", { name: /show live screen/i });
  // Nothing is fetched until the operator asks for it.
  expect(api.evidence).not.toHaveBeenCalled();
  await userEvent.click(toggle);
  await waitFor(() => expect(screen.getByText(/waiting for input/)).toBeInTheDocument());
  expect(api.evidence).toHaveBeenCalledWith(
    "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "screen",
  );
});

test("the feed groups by project so 'which project needs me' is one glance", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [
      action({ id: "a1", project: "agent-sessions" }),
      action({ id: "a2", project: "battlelab-cloud", session_id: "codex:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }),
      action({ id: "a3", project: "agent-sessions", session_id: "kimi:session_cccccccc" }),
    ],
    expired_now: 0,
  });
  renderIt();
  expect(await screen.findByText("agent-sessions")).toBeInTheDocument();
  expect(screen.getByText("battlelab-cloud")).toBeInTheDocument();
  expect(screen.getByText("2 actions")).toBeInTheDocument();
  expect(screen.getByText("1 action")).toBeInTheDocument();
});

test("a failing endpoint degrades to no strip, never a blank Pulse page", async () => {
  vi.mocked(api.orchestrator).mockRejectedValue(new Error("down"));
  const { container } = renderIt();
  await waitFor(() => expect(container.querySelector("section")).toBeNull());
});

test("changing the tier persists it and refreshes the shared config", async () => {
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [],
    expired_now: 0,
  });
  const onTierChange = vi.fn();
  render(
    <MemoryRouter>
      <Orchestrator onTierChange={onTierChange} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole("button", { name: "OFF" }));
  expect(api.setPrefs).toHaveBeenCalledWith({ orchestrator: { autonomy: "off" } });
  // Without the refresh the Settings panel would show pre-save values on remount (#667).
  await waitFor(() => expect(onTierChange).toHaveBeenCalled());
});

test("evidence is re-fetched on every open — 'live' must not mean 'cached once'", async () => {
  // The server serves evidence uncached so the operator always reads the CURRENT screen.
  // Caching the first snapshot client-side quietly defeats that, and shows a screen the
  // session has moved past — exactly what must not be approved against.
  vi.mocked(api.orchestrator).mockResolvedValue({
    config: config(),
    pending: [],
    feed: [action()],
    expired_now: 0,
  });
  vi.mocked(api.evidence)
    .mockResolvedValueOnce({ kind: "screen", text: "first screen", available: true })
    .mockResolvedValueOnce({ kind: "screen", text: "second screen", available: true });
  renderIt();
  const toggle = await screen.findByRole("button", { name: /live screen/i });
  await userEvent.click(toggle);
  await waitFor(() => expect(screen.getByText(/first screen/)).toBeInTheDocument());
  await userEvent.click(toggle); // collapse
  await userEvent.click(toggle); // re-open
  await waitFor(() => expect(screen.getByText(/second screen/)).toBeInTheDocument());
  expect(api.evidence).toHaveBeenCalledTimes(2);
});
