import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import { SessionsProvider } from "../../app/SessionsContext";
import { api } from "../../lib/api";
import fixture from "../../test/roster.fixture.json";
import type { AgentEndpoint, ChatSession, EngineInfo, Session } from "../../types/api";
import { ChatPane } from "./ChatPane";

// #2064: an archived chat-runtime conversation is frozen like an API session — readable, the
// ARCHIVED strip in place of the composer, nothing posted; Unarchive brings the composer back.

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      chatGet: vi.fn(),
      chatSend: vi.fn(),
      chatRetry: vi.fn(),
      agentEndpoint: vi.fn(),
      session: vi.fn(),
      unarchive: vi.fn(),
    },
  };
});

const SID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b";
const KEY = `apichat:${SID}`;
const ENDPOINT: AgentEndpoint = {
  base_url: "https://llm.example.lan/v1",
  model: "qwen3-coder",
  api_key_set: true,
  context_window: 32768,
  max_output_tokens: 4096,
  request_timeout: null,
  configured: true,
  tools: "none",
};

function row(archived: boolean): Session {
  return {
    id: KEY,
    engine: "apichat",
    uuid: SID,
    short_uuid: SID.slice(0, 8),
    cwd: "/w",
    project: { kind: "folder", id: "/w", name: "/w" },
    last_mtime: 1_700_000_000,
    title: "the conversation",
    first_user_message: "hello",
    sticky: false,
    archived,
  } as unknown as Session;
}

const conversation: ChatSession = {
  session_id: SID,
  cwd: "/w",
  created_at: 0,
  turns: [],
  in_flight: null,
};

function renderPane() {
  return render(
    <MemoryRouter>
      <SessionsProvider>
        <ChatPane engine="apichat" id={SID} />
      </SessionsProvider>
    </MemoryRouter>,
  );
}

const composer = () => screen.queryByRole("textbox", { name: "Message the agent" });

beforeEach(() => {
  setRoster(fixture.engines as EngineInfo[], []);
  vi.mocked(api.agentEndpoint).mockResolvedValue(ENDPOINT);
  vi.mocked(api.chatGet).mockResolvedValue(conversation);
});

afterEach(() => {
  vi.clearAllMocks();
  resetRoster();
});

test("an archived conversation shows the strip instead of the composer and posts nothing", async () => {
  vi.mocked(api.session).mockResolvedValue(row(true));
  renderPane();
  expect(await screen.findByTestId("archived-strip")).toBeInTheDocument();
  expect(composer()).toBeNull();
  expect(api.chatSend).not.toHaveBeenCalled();
});

test("Unarchive brings the composer back once the row reads active", async () => {
  const user = userEvent.setup();
  vi.mocked(api.session).mockResolvedValue(row(true));
  let finish!: () => void;
  vi.mocked(api.unarchive).mockImplementation(
    () => new Promise((resolve) => (finish = () => resolve({ id: KEY, archived: false }))),
  );
  renderPane();
  await screen.findByTestId("archived-strip");
  await user.click(screen.getByTestId("archived-unarchive"));
  expect(api.unarchive).toHaveBeenCalledWith(KEY);
  vi.mocked(api.session).mockResolvedValue(row(false));
  await act(async () => finish());
  expect(await screen.findByRole("textbox", { name: "Message the agent" })).toBeInTheDocument();
  expect(screen.queryByTestId("archived-strip")).toBeNull();
  expect(api.chatSend).not.toHaveBeenCalled();
});
