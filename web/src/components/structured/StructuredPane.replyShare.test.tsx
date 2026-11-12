import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../../app/config";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { AppConfig, EngineInfo, StructuredSnapshot, StructuredTurn } from "../../types/api";
import { resetOperatorAvatarCache } from "../../lib/operatorAvatar";
import { StructuredPane } from "./StructuredPane";

// #1956: a finished reply's header carries the share icon; a running or failed one does not.
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

test("only a finished reply offers Share this reply, and it opens the dialog for that turn", async () => {
  vi.mocked(api.structuredSnapshot).mockResolvedValue(
    snap({
      state: "running",
      active_turn: "t3",
      turns: [
        turn({ turn_id: "t1", text: "first", reply: "done one" }),
        turn({ turn_id: "t2", text: "second", reply: "half", state: "failed" }),
        turn({ turn_id: "t3", text: "third", reply: "streaming…", state: "running" }),
      ],
    }),
  );
  renderPane();
  await screen.findByText("done one");
  const buttons = await screen.findAllByTestId("reply-share-button");
  expect(buttons).toHaveLength(1);
  expect(buttons[0]).toHaveAccessibleName("Share this reply");
  await userEvent.click(buttons[0]);
  const dialog = await screen.findByTestId("reply-share");
  expect(dialog).toHaveTextContent("Share this reply");
  await waitFor(() => expect(dialog).toBeVisible());
});
