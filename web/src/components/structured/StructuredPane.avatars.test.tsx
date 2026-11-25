import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../../app/config";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { AppConfig, EngineInfo, StructuredSnapshot, StructuredTurn } from "../../types/api";
import { resetOperatorAvatarCache } from "../../lib/operatorAvatar";
import { StructuredPane } from "./StructuredPane";

// #1931: the disc beside every message in the operator's own API-session pane, and the head's
// "N connected" chip.
vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      structuredSnapshot: vi.fn(),
      structuredEvents: vi.fn(),
      sessions: vi.fn().mockResolvedValue({ sessions: [], total: 0 }),
      avatarBlob: vi.fn(),
    },
  };
});

const ID = "5b0d2c1e-8f3a-4c7d-9e21-6a4b3c2d1e0f";
const KEY = `codex-api:${ID}`;

function turn(over: Partial<StructuredTurn> = {}): StructuredTurn {
  return {
    turn_id: crypto.randomUUID(),
    operation_id: "",
    state: "completed",
    text: "hello",
    reply: "echo:hello",
    text_truncated: false,
    reply_truncated: false,
    reason: null,
    tools: [],
    tools_truncated: false,
    ...over,
  };
}

function snap(over: Partial<StructuredSnapshot> = {}): StructuredSnapshot {
  return {
    session_key: KEY,
    revision: 4,
    event_cursor: 4,
    cwd: "/w",
    state: "idle",
    active_turn: null,
    model_requested: null,
    model_effective: "gpt-5-codex",
    turns: [],
    omitted_turns: 0,
    pending_requests: [],
    native: { native_id: "n", worker: "w1", background_active: false },
    read_only: null,
    ...over,
  };
}

function renderPane(config: Partial<AppConfig> = {}) {
  return render(
    <ConfigCtx.Provider value={{ csrf: "t", username: "marcus", ...config } as AppConfig}>
      <MemoryRouter>
        <StructuredPane engine="codex-api" id={ID} />
      </MemoryRouter>
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  resetOperatorAvatarCache();
  vi.mocked(api.avatarBlob).mockImplementation(async (v: string) => new Blob([v]));
  URL.createObjectURL = vi.fn(() => "blob:operator-avatar");
  URL.revokeObjectURL = vi.fn();
  vi.mocked(api.structuredEvents).mockResolvedValue({
    session_key: KEY,
    revision: 4,
    next_cursor: 4,
    events: [],
  });
});
afterEach(() => {
  vi.clearAllMocks();
  resetRoster();
});

const kinds = (c: HTMLElement) =>
  [...c.querySelectorAll("[data-testid='structured-turn'] [data-avatar]")].map((d) => [
    (d as HTMLElement).dataset.kind,
    d.querySelector("img")?.getAttribute("src") ?? d.textContent,
  ]);

test("you, a visitor, an unreadable sender and the agent each get their disc", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      turns: [
        turn({ text: "mine" }),
        turn({ text: "theirs", author: { name: "jana", via: "public_link" } }),
        turn({ text: "who?", author: { name: null, via: "unknown" }, reply: null }),
      ],
    }),
  );
  const { container } = renderPane();
  await screen.findByText("theirs");
  expect(kinds(container)).toEqual([
    ["accent", "M"],
    ["agent", "🤖"],
    ["who", "J"],
    ["agent", "🤖"],
    ["neutral", "?"],
  ]);
});

test("the operator's uploaded image: fetched once through the seam, shared by every disc", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ turns: [turn({ text: "mine", reply: null }), turn({ text: "again", reply: null })] }),
  );
  const { container } = renderPane({ avatar: "v9" });
  await screen.findByText("again");
  await waitFor(() =>
    expect(kinds(container)).toEqual([
      ["image", "blob:operator-avatar"],
      ["image", "blob:operator-avatar"],
    ]),
  );
  // Never an <img src> on the page's own origin (over Home Free that is the relay's).
  expect(api.avatarBlob).toHaveBeenCalledTimes(1);
  expect(api.avatarBlob).toHaveBeenCalledWith("v9");
});

test("the head counts who is connected once the session is shared, naming them on hover", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ presence: { count: 2, visitors: [{ name: "Jana" }], shared: true } }),
  );
  renderPane();
  const chip = await screen.findByTestId("structured-present");
  expect(chip).toHaveTextContent("2 connected"); // the screen reader's words
  expect(chip.querySelector('[aria-hidden="true"]:not(svg)')?.textContent).toBe("2");
  expect(chip.getAttribute("title")).toContain("Jana · via public link");
});

test("an unshared session nobody else has open shows no count", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({ presence: { count: 1, visitors: [], shared: false }, turns: [turn({ text: "solo" })] }),
  );
  renderPane();
  await screen.findByText("solo");
  expect(screen.queryByTestId("structured-present")).toBeNull();
});
