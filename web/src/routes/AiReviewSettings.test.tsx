import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { api, ApiError } from "../lib/api";
import type { AiReviewConfig, AppConfig, Session } from "../types/api";
import { AiReviewSettings } from "./AiReviewSettings";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
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
    request_timeout: null,
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
    archived: false,
    ...over,
  };
}

function renderPanel(
  block: AiReviewConfig | undefined = aiBlock(),
  refresh: () => void = () => {},
) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    ai_review: block,
  };
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
    ai_review: {
      ...aiBlock(),
      ...(p as { ai_review: object }).ai_review,
      api_key_set: true,
    },
  }));
  vi.mocked(api.aiReviewModels).mockResolvedValue({
    models: ["m-a", "m-b", "minimax-m2.7"],
  });
  vi.mocked(api.sessions).mockResolvedValue({
    sessions: [],
    next_offset: null,
    total: 0,
    facets: { projects: [], engines: [] },
  });
  vi.mocked(api.reviewExclude).mockResolvedValue({
    id: "claude:a",
    review_excluded: false,
  });
});

test("renders the config from /api/config and never echoes a key (write-only)", async () => {
  renderPanel();
  expect(
    await screen.findByRole("heading", { name: "AI endpoint" }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText(/Endpoint base URL/i)).toHaveValue(
    "https://ai.example.io/v1",
  );
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
    expect(screen.getByLabelText("Model").getAttribute("placeholder")).toBe(
      "model id",
    ),
  );
  expect(
    screen.getByText(/doesn’t list models — enter the model id manually/i),
  ).toBeInTheDocument();
});

test("a plain visit with a stored config stays quiet — no dirty note, no status line (#543)", async () => {
  // The mount probe populates the dropdown but must not wear save-validation clothes:
  // no "Validating endpoint…", no "✓ Endpoint validated", and mount-only behavior can
  // never produce a dirty key draft.
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" }); // mount probe done
  expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/Validating endpoint/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/Endpoint validated/i)).not.toBeInTheDocument();
  expect(screen.getByLabelText(/API key/i)).toHaveValue("");
  expect(
    screen.getByRole("button", { name: /save & validate/i }),
  ).toBeDisabled();
});

test("the API-key field opts out of password-manager autofill (#543)", () => {
  // autocomplete="off" is ignored for stored credentials — Chrome fills the app's login
  // password into the field on load, dirtying the form one click away from overwriting
  // the stored API key. "new-password" is the standard suppression signal.
  renderPanel();
  expect(screen.getByLabelText(/API key/i)).toHaveAttribute(
    "autocomplete",
    "new-password",
  );
});

test("a failed mount probe still surfaces the gateway error on a plain visit (#543)", async () => {
  // Only the in-flight/success status goes quiet on mount — a broken stored endpoint
  // must stay visible.
  const gw = "model listing returned HTTP 401: key rejected.";
  vi.mocked(api.aiReviewModels).mockRejectedValue(new ApiError(502, gw));
  renderPanel();
  expect(await screen.findByText(`✗ ${gw}`)).toBeInTheDocument();
  expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
});

test("model list is not fetched while unconfigured (no endpoint/key yet)", async () => {
  renderPanel(aiBlock({ configured: false, api_key_set: false, base_url: "" }));
  await screen.findByRole("heading", { name: "AI endpoint" });
  expect(api.aiReviewModels).not.toHaveBeenCalled();
  expect(
    screen.getByText(/Set the base URL and API key first/i),
  ).toBeInTheDocument();
});

test("Save & validate persists URL+key together, probes /models, and confirms", async () => {
  // #394: the endpoint section saves ONLY via the explicit button — one setPrefs call
  // carrying both fields — and validates immediately through the /models proxy.
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" }); // mount probe done
  expect(api.aiReviewModels).toHaveBeenCalledTimes(1);

  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  const key = screen.getByLabelText(/API key/i);
  await user.type(key, "sk-new-key");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: {
        base_url: "https://other.example/v1",
        api_key: "sk-new-key",
      },
    }),
  );
  expect(api.setPrefs).toHaveBeenCalledTimes(1); // both fields in ONE save
  // The save-time validation probe bypasses the server cache.
  await waitFor(() =>
    expect(api.aiReviewModels).toHaveBeenLastCalledWith({ refresh: true }),
  );
  await waitFor(() => expect(key).toHaveValue("")); // write-only: cleared after save
  expect(
    await screen.findByText(/Endpoint validated — 3 models available/i),
  ).toBeInTheDocument();
});

test("the URL and key fields never persist on blur", async () => {
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.tab(); // blur the URL — nothing saved
  const key = screen.getByLabelText(/API key/i);
  await user.type(key, "sk-typed-but-not-saved");
  await user.tab(); // blur the key — NEVER persisted on blur (#394)
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(key).toHaveValue("sk-typed-but-not-saved"); // draft survives until Save
});

test("a failed validation shows the gateway's error verbatim (#382)", async () => {
  const user = userEvent.setup();
  const gw =
    "model listing returned HTTP 401: Authentication Error - LiteLLM Virtual Key expected.";
  vi.mocked(api.aiReviewModels)
    .mockResolvedValueOnce({ models: ["m-a"] }) // mount probe: stored config still valid
    .mockRejectedValueOnce(new ApiError(502, gw)); // save-time probe: new key rejected
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  await user.type(screen.getByLabelText(/API key/i), "not-a-virtual-key");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(await screen.findByText(`✗ ${gw}`)).toBeInTheDocument();
  // The model field falls back to free-text entry; the config itself stayed saved.
  expect(screen.getByLabelText("Model").getAttribute("placeholder")).toBe(
    "model id",
  );
});

test("dirty endpoint edits show the unsaved note and lock the model control", async () => {
  const user = userEvent.setup();
  // No validated config: the mount probe fails (e.g. stored key already broken).
  vi.mocked(api.aiReviewModels).mockRejectedValue(
    new ApiError(502, "HTTP 401"),
  );
  renderPanel();
  await waitFor(() =>
    expect(screen.getByLabelText("Model").getAttribute("placeholder")).toBe(
      "model id",
    ),
  );
  const saveBtn = screen.getByRole("button", { name: /save & validate/i });
  expect(saveBtn).toBeDisabled(); // nothing edited yet
  await user.type(screen.getByLabelText(/API key/i), "sk-fresh");
  expect(
    screen.getByText(/Unsaved changes — Save applies and validates/i),
  ).toBeInTheDocument();
  expect(saveBtn).toBeEnabled();
  expect(screen.getByLabelText("Model")).toBeDisabled(); // no validated config → locked
  expect(
    screen.getByRole("button", { name: /refresh model list/i }),
  ).toBeDisabled();
});

test("dirty edits do NOT lock the model dropdown while a validated config exists", async () => {
  const user = userEvent.setup();
  renderPanel(); // mount probe succeeds → validated
  const select = await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();
  expect(select).toBeEnabled(); // the saved config behind the list is still validated
});

test("dirty endpoint drafts survive a model auto-save while a validated config exists", async () => {
  // Hermes on #396: the dropdown stays enabled next to dirty endpoint edits (#394), so
  // the model save's echo must not reseed the drafts and silently discard the edits.
  const user = userEvent.setup();
  renderPanel(); // mount probe succeeds → validated
  const select = await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.type(screen.getByLabelText(/API key/i), "sk-unsaved-edit");
  await user.selectOptions(select, "m-b");
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { model: "m-b" } }),
  );
  expect(url).toHaveValue("https://other.example/v1"); // NOT reverted to the saved URL
  expect(screen.getByLabelText(/API key/i)).toHaveValue("sk-unsaved-edit");
  expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();
});

test("a successful Save & validate still reseeds — the dirty state clears", async () => {
  // The #396 guard must not overshoot: the endpoint's own save echoes the draft back
  // as the persisted URL, so the reseed applies and the unsaved warning goes away.
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  await waitFor(() =>
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument(),
  );
  expect(url).toHaveValue("https://other.example/v1"); // the new persisted value
  expect(
    screen.getByRole("button", { name: /save & validate/i }),
  ).toBeDisabled();
});

test("the masked sentinel round-trips as 'unchanged' — never sent as the key", async () => {
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.type(screen.getByLabelText(/API key/i), "********");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { base_url: "https://other.example/v1" }, // no api_key field at all
    }),
  );
});

test("a save that leaves the config incomplete reports it instead of probing", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockImplementation(async (p) => ({
    ai_review: {
      ...aiBlock({ api_key_set: false, configured: false }),
      ...(p as { ai_review: object }).ai_review,
    },
  }));
  renderPanel(aiBlock({ configured: false, api_key_set: false, base_url: "" }));
  await screen.findByRole("heading", { name: "AI endpoint" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.type(url, "https://other.example/v1");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(
    await screen.findByText(
      /Set both the base URL and an API key to validate/i,
    ),
  ).toBeInTheDocument();
  expect(api.aiReviewModels).not.toHaveBeenCalled(); // nothing to validate yet
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
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { prompt: "my new prompt" },
    }),
  );
  await user.click(screen.getByRole("button", { name: /reset to default/i }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { prompt: DEFAULT_PROMPT },
    }),
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
  await waitFor(() =>
    expect(api.reviewExclude).toHaveBeenCalledWith("claude:a", false),
  );
  await waitFor(() =>
    expect(screen.queryByText("rotate creds")).not.toBeInTheDocument(),
  );
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
  await waitFor(() =>
    expect(screen.queryByText("set")).not.toBeInTheDocument(),
  );
  expect(
    screen.queryByRole("button", { name: "Remove key" }),
  ).not.toBeInTheDocument();
});

test("Remove key is not offered while no key is stored", async () => {
  renderPanel(aiBlock({ api_key_set: false, configured: false }));
  await screen.findByRole("heading", { name: "AI endpoint" });
  expect(
    screen.queryByRole("button", { name: "Remove key" }),
  ).not.toBeInTheDocument();
});

test("completing the endpoint config refetches the shared /api/config context", async () => {
  // Hermes #367: the sidebar's Review now/exclude gating reads the one-time /api/config
  // snapshot — a save that flips `configured` must trigger a context refresh.
  const user = userEvent.setup();
  const refresh = vi.fn();
  vi.mocked(api.setPrefs).mockResolvedValue({ ai_review: aiBlock() }); // configured: true
  renderPanel(aiBlock({ configured: false, api_key_set: false }), refresh);
  await user.type(screen.getByLabelText(/API key/i), "sk-new-key");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
});

test("a save that doesn't flip `configured` leaves the config context alone", async () => {
  const user = userEvent.setup();
  const refresh = vi.fn();
  renderPanel(aiBlock(), refresh); // already configured; the echo stays configured
  await user.click(
    screen.getByRole("checkbox", { name: /enable periodic reviews/i }),
  );
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  expect(refresh).not.toHaveBeenCalled();
});

test("review timeout renders the saved value; empty shows the 120s default hint", async () => {
  renderPanel(aiBlock({ request_timeout: 90 }));
  expect(await screen.findByLabelText("Request timeout")).toHaveValue(90);
  expect(
    screen.getByText(/Slow local models often need 60–180s/i),
  ).toBeInTheDocument();
});

test("review timeout commits on blur through the ai_review patch flow", async () => {
  const user = userEvent.setup();
  renderPanel();
  const field = screen.getByLabelText("Request timeout");
  expect(field).toHaveValue(null); // unset → placeholder shows the 120 default
  await user.type(field, "240");
  await user.tab();
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { request_timeout: 240 },
    }),
  );
});

test("an out-of-range review timeout is rejected client-side and the draft reverts", async () => {
  const user = userEvent.setup();
  renderPanel(aiBlock({ request_timeout: 90 }));
  const field = screen.getByLabelText("Request timeout");
  await user.clear(field);
  await user.type(field, "5");
  await user.tab();
  expect(api.setPrefs).not.toHaveBeenCalledWith(
    expect.objectContaining({
      ai_review: expect.objectContaining({
        request_timeout: expect.anything(),
      }),
    }),
  );
  expect(field).toHaveValue(90); // reverted to the saved value, like interval
});

test("clearing the review timeout sends null (unset → env/default applies)", async () => {
  const user = userEvent.setup();
  renderPanel(aiBlock({ request_timeout: 90 }));
  const field = screen.getByLabelText("Request timeout");
  await user.clear(field);
  await user.tab();
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { request_timeout: null },
    }),
  );
});

test("the enable toggle persists immediately", async () => {
  const user = userEvent.setup();
  renderPanel();
  await user.click(
    screen.getByRole("checkbox", { name: /enable periodic reviews/i }),
  );
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: { enabled: true } }),
  );
});
