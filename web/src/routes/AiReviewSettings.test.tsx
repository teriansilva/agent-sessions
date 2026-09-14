import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
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

function aiBlock(over: Partial<AiReviewConfig> = {}): AiReviewConfig {
  return {
    enabled: false,
    base_url: "https://ai.example.io/v1",
    model: "minimax-m2.7",
    interval_minutes: 5,
    max_input_chars: 24000,
    request_timeout: null,
    api_key_set: true,
    configured: true,
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
  // Both views, as the two Settings pages mount them (#956). A router, because the prompt link
  // is an in-app link to the Prompts page.
  return render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={refresh}>
        <ConfigCtx.Provider value={config as AppConfig}>
          <AiReviewSettings view="endpoint" />
          <AiReviewSettings view="review" />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
    </MemoryRouter>,
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

/** With a key on file the panel shows a static readout, not an input — a field that isn't
 *  on the page can't be autofilled (#834). Click "Replace key" to put one there. */
async function revealKeyField(user: ReturnType<typeof userEvent.setup>) {
  const replace = screen.queryByRole("button", { name: /replace key/i });
  if (replace) await user.click(replace);
  return screen.getByLabelText(/API key/i);
}

test("renders the config from /api/config and never echoes a key (write-only)", async () => {
  const user = userEvent.setup();
  renderPanel();
  expect(
    await screen.findByRole("heading", { name: "AI endpoint" }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText(/Endpoint base URL/i)).toHaveValue(
    "https://ai.example.io/v1",
  );
  // A stored key shows as a readout + SET badge, with no fillable field at all (#834)…
  expect(screen.getByText("set")).toBeInTheDocument();
  expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument();
  // …and the field revealed by "Replace key" is empty (the value never round-trips).
  const key = await revealKeyField(user);
  expect(key).toHaveValue("");
  expect(key).toHaveAttribute("type", "password");
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
  // Structurally quiet: with a key stored there is no input to hold a phantom draft (#834).
  expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /save & validate/i }),
  ).toBeDisabled();
});

test("the API-key field opts out of password-manager autofill (#543)", async () => {
  // autocomplete="off" is ignored for stored credentials — a browser fills a saved password
  // into the field on load, dirtying the form one click away from overwriting the stored API
  // key. "new-password" is the standard suppression signal; it rides the transient field the
  // "Replace key" reveal puts on the page (#834), alongside the vendor opt-outs.
  const user = userEvent.setup();
  renderPanel();
  const key = await revealKeyField(user);
  expect(key).toHaveAttribute("autocomplete", "new-password");
  expect(key).toHaveAttribute("data-1p-ignore");
  expect(key).toHaveAttribute("data-lpignore");
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
  const key = await revealKeyField(user);
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
  // Write-only: the draft is cleared AND the field folds back to the readout (#834), so
  // there is nothing left on the page for a password manager to refill.
  await waitFor(() =>
    expect(screen.queryByLabelText(/API key/i)).not.toBeInTheDocument(),
  );
  expect(
    await screen.findByText(/Endpoint validated — 3 models available/i),
  ).toBeInTheDocument();
});

test("a key typed WHILE a save is in flight survives that save's success (#836)", async () => {
  // The endpoint controls stay editable for the whole request on purpose — the /models
  // probe can run for minutes against a slow gateway, so locking them would be worse. That
  // means the user can type a newer key before the response lands, and the success
  // continuation used to clear the field unconditionally, silently discarding it.
  const user = userEvent.setup();
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockReturnValueOnce(
    new Promise((r) => {
      release = r;
    }) as ReturnType<typeof api.setPrefs>,
  );
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  await user.type(await revealKeyField(user), "sk-first");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  await screen.findByText(/Saving…/i);

  // …the user thinks better of it and types a different key while A is in flight.
  const field = screen.getByLabelText(/API key/i);
  await user.clear(field);
  await user.type(field, "sk-second");

  release({ ai_review: aiBlock() }); // A finally succeeds
  await waitFor(() =>
    expect(screen.queryByText(/Saving…/i)).not.toBeInTheDocument(),
  );

  // The newer key is still there, still unsaved — one more Save stores it. Not discarded.
  expect(screen.getByLabelText(/API key/i)).toHaveValue("sk-second");
  expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /save & validate/i }),
  ).toBeEnabled();
});

test("the URL and key fields never persist on blur", async () => {
  const user = userEvent.setup();
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  const url = screen.getByLabelText(/Endpoint base URL/i);
  await user.clear(url);
  await user.type(url, "https://other.example/v1");
  await user.tab(); // blur the URL — nothing saved
  const key = await revealKeyField(user);
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
  await user.type(await revealKeyField(user), "not-a-virtual-key");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));
  expect(await screen.findByText(`✗ ${gw}`)).toBeInTheDocument();
  // The model field falls back to free-text entry; the config itself stayed saved.
  expect(screen.getByLabelText("Model").getAttribute("placeholder")).toBe(
    "model id",
  );
});

test("a REJECTED save reports the server's reason — the dirty warning never buries it (#834)", async () => {
  // The reported bug. A rejected save keeps the typed key on purpose, so the form stays
  // dirty — and with `endpointDirty` tested ahead of the error state the error branch was
  // unreachable: every failure rendered as a bare "● Unsaved changes" and the user read it
  // as "it can't save a new key", with no reason anywhere on the page.
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValueOnce(
    new ApiError(422, "ai_review.base_url must be an http(s) URL"),
  );
  renderPanel();
  await screen.findByRole("combobox", { name: "Model" });
  await user.type(await revealKeyField(user), "sk-rejected");
  await user.click(screen.getByRole("button", { name: /save & validate/i }));

  expect(
    await screen.findByText("✗ ai_review.base_url must be an http(s) URL"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();
  // The typed key survives, so a retry doesn't mean retyping the secret.
  expect(screen.getByLabelText(/API key/i)).toHaveValue("sk-rejected");

  // The verdict belongs to the rejected values: the next edit retires it and the dirty
  // warning takes over again, so a stale error can't stay pinned to text that changed.
  await user.type(screen.getByLabelText(/Endpoint base URL/i), "x");
  expect(
    screen.queryByText(/ai_review\.base_url must be an http/),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/Unsaved changes/i)).toBeInTheDocument();
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
  await user.type(await revealKeyField(user), "sk-fresh");
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
  await user.type(await revealKeyField(user), "sk-unsaved-edit");
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
  await user.type(await revealKeyField(user), "********");
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

test("the review prompt is edited in the Prompts catalog, not here (#824)", async () => {
  renderPanel();
  // One editor per value: this panel owns the ENDPOINT, the catalog owns the prompts.
  expect(screen.queryByRole("textbox", { name: "Review prompt" })).toBeNull();
  const link = screen.getByRole("link", { name: /prompts → tail review/i });
  expect(link).toHaveAttribute("href", "/settings/ai-prompts#prompt-tail_review");
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

test("every successful save refreshes the config context, not only a `configured` flip (#956)", async () => {
  // One page per section makes a remount on navigation routine. A save that left the shared
  // context stale would show the pre-save value on the next visit — the #667 failure mode — so
  // the refresh no longer waits for `configured` to change.
  const user = userEvent.setup();
  const refresh = vi.fn();
  renderPanel(aiBlock(), refresh); // already configured; the echo stays configured
  await user.click(
    screen.getByRole("checkbox", { name: /enable periodic reviews/i }),
  );
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
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

/** Only the Session review page, as Settings mounts it (#956). */
function renderReview(block: AiReviewConfig = aiBlock()) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    ai_review: block,
  };
  return render(
    <MemoryRouter>
      <ConfigRefreshCtx.Provider value={() => {}}>
        <ConfigCtx.Provider value={config as AppConfig}>
          <AiReviewSettings view="review" />
        </ConfigCtx.Provider>
      </ConfigRefreshCtx.Provider>
    </MemoryRouter>,
  );
}

test("Session review page: a rejected interval says why, and the rejected value stays visible for correction (#957)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValue(
    new ApiError(422, "ai_review.interval_minutes must be an integer between 1 and 1440"),
  );
  renderReview();
  const interval = screen.getByLabelText("Review every");
  await user.clear(interval);
  await user.type(interval, "999999");
  await user.tab();
  expect(
    await screen.findByRole("alert"),
  ).toHaveTextContent("ai_review.interval_minutes must be an integer between 1 and 1440");
  expect(interval).toHaveValue(999999);
});

test("Session review page: a rejected enable toggle says why and the checkbox keeps the saved state (#957)", async () => {
  const user = userEvent.setup();
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("network down"));
  renderReview(aiBlock({ enabled: false }));
  const toggle = screen.getByRole("checkbox", { name: /enable periodic reviews/i });
  await user.click(toggle);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Couldn’t save — please try again.",
  );
  expect(toggle).not.toBeChecked();
});

test("Session review page: a successful save says Saved. on this page", async () => {
  const user = userEvent.setup();
  renderReview(aiBlock({ enabled: false }));
  await user.click(screen.getByRole("checkbox", { name: /enable periodic reviews/i }));
  expect(await screen.findByText("Saved.")).toBeInTheDocument();
});
