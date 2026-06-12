import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api } from "../lib/api";
import type { AiReviewConfig, AppConfig, Session } from "../types/api";
import { AiReviewSettings } from "./AiReviewSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      setPrefs: vi.fn(),
      aiReviewModels: vi.fn(),
      sessions: vi.fn(),
      reviewExclude: vi.fn(),
    },
  };
});

const DEFAULT_PROMPT = "default review prompt";

function aiBlock(over: Partial<AiReviewConfig> = {}): AiReviewConfig {
  return {
    enabled: false,
    base_url: "https://ai.example.io/v1",
    model: "minimax-m2.7",
    interval_minutes: 5,
    prompt: "custom prompt",
    max_input_chars: 24000,
    api_key_set: true,
    configured: true,
    default_prompt: DEFAULT_PROMPT,
    ...over,
  };
}

function sess(id: string, title: string, over: Partial<Session> = {}): Session {
  return {
    id,
    engine: "claude",
    uuid: id.split(":")[1],
    short_uuid: id.slice(0, 8),
    cwd: "/home/m/x",
    project: "/home/m/x",
    last_mtime: 1000,
    first_user_message: "",
    title,
    sticky: false,
    sort_key: 0,
    archived: false,
    ...over,
  };
}

function renderPanel(
  block: AiReviewConfig | undefined = aiBlock(),
  refresh: () => void = () => {},
) {
  const config = { csrf: "t", new_session_engines: [], terminal_backend: "ws", ai_review: block };
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config as AppConfig}>
        <AiReviewSettings />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.setPrefs).mockImplementation(async (p) => ({
    ai_review: { ...aiBlock(), ...(p as { ai_review: object }).ai_review, api_key_set: true },
  }));
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: ["m-a", "m-b", "minimax-m2.7"] });
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [],
    next_offset: null,
    total: 0,
    facets: { projects: [], engines: [] },
  });
  vi.mocked(api.reviewExclude).mockResolvedValue({ id: "claude:a", review_excluded: false });
});

test("renders the config from /api/config and never echoes a key (write-only)", async () => {
  renderPanel();
  expect(await screen.findByRole("heading", { name: "AI session review" })).toBeInTheDocument();
  expect(screen.getByLabelText(/Endpoint base URL/i)).toHaveValue("https://ai.example.io/v1");
  // The key field is empty (value never round-trips); a SET badge marks a stored key.
  const key = screen.getByLabelText(/API key/i);
  expect(key).toHaveValue("");
  expect(key).toHaveAttribute("type", "password");
  expect(screen.getByText("set")).toBeInTheDocument();
});

test("model dropdown loads via the server-side proxy; picking one saves it", async () => {
  const user = userEvent.setup();
  renderPanel();
  const select = await screen.findByRole("combobox", { name: "Model" });
  expect(api.aiReviewModels).toHaveBeenCalledTimes(1);
  await user.selectOptions(select, "m-b");
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { model: "m-b" } }),
  );
});

test("the refresh button re-fetches the model list bypassing the cache", async () => {
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  await user.click(screen.getByRole("button", { name: /refresh model list/i }));
  await waitFor(() =>
    expect(api.aiReviewModels).toHaveBeenLastCalledWith({ refresh: true }),
  );
});

test("falls back to free-text model entry when the endpoint can't list models", async () => {
  vi.mocked(api.aiReviewModels).mockRejectedValue(new Error("502"));
  renderPanel();
  await waitFor(() =>
    expect(screen.getByLabelText("Model").getAttribute("placeholder")).toBe("model id"),
  );
  expect(
    screen.getByText(/doesn’t list models — enter the model id manually/i),
  ).toBeInTheDocument();
});

test("model list is not fetched while unconfigured (no endpoint/key yet)", async () => {
  renderPanel(aiBlock({ configured: false, api_key_set: false, base_url: "" }));
  await screen.findByRole("heading", { name: "AI session review" });
  expect(api.aiReviewModels).not.toHaveBeenCalled();
  expect(screen.getByText(/Set the base URL and API key first/i)).toBeInTheDocument();
});

test("base URL commits on blur; a new API key is sent once and the field clears", async () => {
  const user = userEvent.setup();
  renderPanel();
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.tab();
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { base_url: "https://other.example/v1" },
    }),
  );

  const key = screen.getByLabelText(/API key/i);
  await user.type(key, "sk-new-key");
  await user.tab();
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { api_key: "sk-new-key" } }),
  );
  await waitFor(() => expect(key).toHaveValue("")); // write-only: cleared after save
});

test("blurring an empty key field sends nothing (blank = unchanged)", async () => {
  const user = userEvent.setup();
  renderPanel();
  await user.click(screen.getByLabelText(/API key/i));
  await user.tab();
  expect(api.setPrefs).not.toHaveBeenCalledWith(
    expect.objectContaining({ ai_review: expect.objectContaining({ api_key: "" }) }),
  );
});

test("prompt Save persists the draft; Reset to default saves the server default", async () => {
  const user = userEvent.setup();
  renderPanel();
  const area = screen.getByRole("textbox", { name: "Review prompt" });
  expect(area).toHaveValue("custom prompt");
  await user.clear(area);
  await user.type(area, "my new prompt");
  await user.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { prompt: "my new prompt" } }),
  );
  await user.click(screen.getByRole("button", { name: /reset to default/i }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { prompt: DEFAULT_PROMPT } }),
  );
  expect(area).toHaveValue(DEFAULT_PROMPT);
});

test("excluded sessions list re-includes a session", async () => {
  const user = userEvent.setup();
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [
      sess("claude:a", "rotate creds", { review_excluded: true }),
      sess("claude:b", "not excluded"),
    ],
    next_offset: null,
    total: 2,
    facets: { projects: [], engines: [] },
  });
  renderPanel();
  expect(await screen.findByText("rotate creds")).toBeInTheDocument();
  expect(screen.queryByText("not excluded")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Include" }));
  await waitFor(() => expect(api.reviewExclude).toHaveBeenCalledWith("claude:a", false));
  await waitFor(() => expect(screen.queryByText("rotate creds")).not.toBeInTheDocument());
});

test("Remove key sends api_key: null, clears the badge, and the action disappears", async () => {
  const user = userEvent.setup();
  // The server echo after an explicit clear: key removed → no longer configured.
  vi.mocked(api.setPrefs).mockResolvedValue({
    ai_review: aiBlock({ api_key_set: false, configured: false }),
  });
  renderPanel();
  await user.click(screen.getByRole("button", { name: "Remove key" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { api_key: null } }),
  );
  await waitFor(() => expect(screen.queryByText("set")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Remove key" })).not.toBeInTheDocument();
});

test("Remove key is not offered while no key is stored", async () => {
  renderPanel(aiBlock({ api_key_set: false, configured: false }));
  await screen.findByRole("heading", { name: "AI session review" });
  expect(screen.queryByRole("button", { name: "Remove key" })).not.toBeInTheDocument();
});

test("completing the endpoint config refetches the shared /api/config context", async () => {
  // Hermes #367: the sidebar's Review now/exclude gating reads the one-time /api/config
  // snapshot — a save that flips `configured` must trigger a context refresh.
  const user = userEvent.setup();
  const refresh = vi.fn();
  vi.mocked(api.setPrefs).mockResolvedValue({ ai_review: aiBlock() }); // configured: true
  renderPanel(aiBlock({ configured: false, api_key_set: false }), refresh);
  await user.type(screen.getByLabelText(/API key/i), "sk-new-key");
  await user.tab();
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
});

test("a save that doesn't flip `configured` leaves the config context alone", async () => {
  const user = userEvent.setup();
  const refresh = vi.fn();
  renderPanel(aiBlock(), refresh); // already configured; the echo stays configured
  await user.click(screen.getByRole("checkbox", { name: /enable periodic reviews/i }));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  expect(refresh).not.toHaveBeenCalled();
});

test("the enable toggle persists immediately", async () => {
  const user = userEvent.setup();
  renderPanel();
  await user.click(screen.getByRole("checkbox", { name: /enable periodic reviews/i }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { enabled: true } }),
  );
});
