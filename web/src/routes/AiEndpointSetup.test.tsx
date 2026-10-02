import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { resetEndpointStatus } from "../lib/aiEndpointStatus";
import { api, ApiError } from "../lib/api";
import type { AiReviewConfig, AppConfig } from "../types/api";
import { AiEndpointSetup } from "./AiEndpointSetup";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      setPrefs: vi.fn(),
      aiReviewModels: vi.fn(),
      testAiEndpoint: vi.fn(),
    },
  };
});

type Listing = { models: string[]; listing: "ok" | "unsupported" };

function aiBlock(over: Partial<AiReviewConfig> = {}): AiReviewConfig {
  return {
    enabled: false,
    base_url: "https://ai.example.io/v1",
    model: "m-a",
    interval_minutes: 5,
    max_input_chars: 24000,
    request_timeout: null,
    api_key_set: true,
    configured: true,
    ...over,
  };
}

const UNCONFIGURED = aiBlock({
  base_url: "",
  model: "",
  api_key_set: false,
  configured: false,
});

/** The server's public echo for a patch applied to `base`. */
function echo(
  patch: Record<string, unknown>,
  base: AiReviewConfig = aiBlock(),
): { ai_review: AiReviewConfig } {
  const next: AiReviewConfig = {
    ...base,
    ...(typeof patch.base_url === "string" ? { base_url: patch.base_url } : {}),
    ...(typeof patch.model === "string" ? { model: patch.model } : {}),
    ...("request_timeout" in patch
      ? { request_timeout: patch.request_timeout as number | null }
      : {}),
    ...("api_key" in patch ? { api_key_set: patch.api_key !== null } : {}),
  };
  next.configured = next.base_url !== "" && next.api_key_set;
  return { ai_review: next };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSetup(block: AiReviewConfig = aiBlock(), refresh = vi.fn()) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    ai_review: block,
  } as AppConfig;
  const utils = render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <AiEndpointSetup />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
  return { ...utils, refresh };
}

const baseUrl = () => screen.getByLabelText(/Base URL/i);
const apiKey = () => screen.getByLabelText(/API key/i);
const saveConnection = () =>
  screen.getByRole("button", { name: "Save connection" });
const connected = () => screen.findByText(/✓ Connected — 2 models available/);

beforeEach(() => {
  resetEndpointStatus();
  vi.mocked(api.aiReviewModels)
    .mockReset()
    .mockResolvedValue({ models: ["m-a", "m-b"] });
  vi.mocked(api.testAiEndpoint)
    .mockReset()
    .mockResolvedValue({ models: ["m-a", "m-b"], listing: "ok" });
  vi.mocked(api.setPrefs)
    .mockReset()
    .mockImplementation(async (p) =>
      echo((p as { ai_review: Record<string, unknown> }).ai_review),
    );
});

// ---- a plain visit ----------------------------------------------------------------------

test("a plain visit lists the saved connection quietly — a readout, no key field, nothing unsaved", async () => {
  renderSetup();
  expect(await connected()).toBeInTheDocument();
  expect(screen.getByText(/^\*+ stored$/)).toBeInTheDocument();
  expect(document.querySelector('input[type="password"]')).toBeNull();
  expect(screen.queryByText(/Unsaved/)).toBeNull();
  expect(api.aiReviewModels).toHaveBeenCalledTimes(1);
  expect(api.setPrefs).not.toHaveBeenCalled();
  const strip = screen.getByTestId("endpoint-status");
  expect(strip).toHaveTextContent("ai.example.io");
  expect(strip).toHaveTextContent("Connected");
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("m-a");
});

test("Replace key reveals an empty field that password managers are told to leave alone (#834/#543)", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.click(screen.getByRole("button", { name: "Replace key" }));
  const key = apiKey();
  expect(key).toHaveValue("");
  expect(key).toHaveAttribute("autocomplete", "new-password");
  expect(key).toHaveAttribute("data-1p-ignore", "true");
  expect(key).toHaveAttribute("data-lpignore", "true");
  expect(key).toHaveAttribute("data-bwignore", "true");
});

test("a failed check of the saved connection says why and turns the status to Check failed", async () => {
  vi.mocked(api.aiReviewModels).mockRejectedValue(
    new ApiError(502, "Virtual Key expected"),
  );
  renderSetup();
  expect(await screen.findByText("✗ Virtual Key expected")).toBeInTheDocument();
  expect(screen.getByTestId("endpoint-status")).toHaveTextContent(
    "Check failed",
  );
});

// ---- 01 Connection ----------------------------------------------------------------------

test("Save connection checks the draft first, then stores exactly what it checked", async () => {
  const user = userEvent.setup();
  const { refresh } = renderSetup(UNCONFIGURED);
  await user.type(baseUrl(), "https://ai.example.io/v1");
  await user.type(apiKey(), "sk-new");
  await user.click(saveConnection());

  const body = { base_url: "https://ai.example.io/v1", api_key: "sk-new" };
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({ ai_review: body }),
  );
  expect(api.testAiEndpoint).toHaveBeenCalledWith(body);
  expect(
    vi.mocked(api.testAiEndpoint).mock.invocationCallOrder[0],
  ).toBeLessThan(vi.mocked(api.setPrefs).mock.invocationCallOrder[0]);
  expect(await connected()).toBeInTheDocument();
  // Write-only: the field folds back to the readout once the key is stored.
  expect(screen.getByText(/^\*+ stored$/)).toBeInTheDocument();
  expect(refresh).toHaveBeenCalled();
  // The check's own listing publishes the model list — no second request.
  expect(api.aiReviewModels).not.toHaveBeenCalled();
});

test("a failed check saves nothing, says why, and offers Save without testing", async () => {
  const user = userEvent.setup();
  vi.mocked(api.testAiEndpoint).mockRejectedValue(
    new ApiError(502, "gateway unreachable"),
  );
  renderSetup(UNCONFIGURED);
  await user.type(baseUrl(), "https://ai.example.io/v1");
  await user.type(apiKey(), "sk-x");
  await user.click(saveConnection());

  expect(
    await screen.findByText("✗ Not saved — gateway unreachable"),
  ).toBeInTheDocument();
  expect(api.setPrefs).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "Save without testing" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { base_url: "https://ai.example.io/v1", api_key: "sk-x" },
    }),
  );
  expect(await screen.findByText("Saved without testing.")).toBeInTheDocument();
});

test("a request the SERVER refuses offers no Save without testing — it would be refused again", async () => {
  const user = userEvent.setup();
  vi.mocked(api.testAiEndpoint).mockRejectedValue(
    new ApiError(422, "base_url must be an http(s) URL"),
  );
  renderSetup(UNCONFIGURED);
  await user.type(baseUrl(), "https://ai.example.io/v1");
  await user.type(apiKey(), "sk-x");
  await user.click(saveConnection());
  expect(
    await screen.findByText(/Not saved — base_url must be an http\(s\) URL/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Save without testing" }),
  ).toBeNull();
});

test("an endpoint that can't list models is saved, and step 02 takes a typed id", async () => {
  const user = userEvent.setup();
  vi.mocked(api.testAiEndpoint).mockResolvedValue({
    models: [],
    listing: "unsupported",
  });
  renderSetup(UNCONFIGURED);
  await user.type(baseUrl(), "https://gw.example.org/openai");
  await user.type(apiKey(), "sk-x");
  await user.click(saveConnection());
  expect(
    await screen.findByText(/Saved — this endpoint doesn’t list models; type the model id below/),
  ).toBeInTheDocument();
  expect(api.setPrefs).toHaveBeenCalledTimes(1);
  const model = screen.getByRole("textbox", { name: "Model" });
  expect(model).toBeEnabled();
});

test("moving to another host asks for that host's key before Test or Save, and Test sends it", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://other.example.org/v1");

  expect(screen.getByText(/New host — enter its API key/)).toBeInTheDocument();
  expect(saveConnection()).toBeDisabled();
  expect(screen.getByRole("button", { name: "Test" })).toBeDisabled();

  await user.type(apiKey(), "sk-other");
  await user.click(screen.getByRole("button", { name: "Test" }));
  expect(api.testAiEndpoint).toHaveBeenCalledWith({
    base_url: "https://other.example.org/v1",
    api_key: "sk-other",
  });
  expect(await screen.findByText(/Not saved yet/)).toBeInTheDocument();
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("the same host needs no key: the stored one is used for it", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  expect(screen.queryByText(/New host/)).toBeNull();
  await user.click(screen.getByRole("button", { name: "Test" }));
  expect(api.testAiEndpoint).toHaveBeenCalledWith({
    base_url: "https://ai.example.io/v2",
  });
});

test("Remove key sends api_key: null, and the key field returns", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.click(screen.getByRole("button", { name: "Remove key" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { api_key: null },
    }),
  );
  expect(await screen.findByPlaceholderText("sk-…")).toBeInTheDocument();
});

// ---- 02 Model ---------------------------------------------------------------------------

test("the model step is locked, and says why, while the connection has unsaved edits", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.type(baseUrl(), "x");
  expect(
    screen.getByText(/Save the connection first — this list belongs to the saved endpoint/),
  ).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Model" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save model" })).toBeDisabled();
});

test("choosing a model does not save it; Save model stores model and timeout together", async () => {
  const user = userEvent.setup();
  renderSetup();
  const combo = await screen.findByRole("combobox", { name: "Model" });
  await user.selectOptions(combo, "m-b");
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(
    screen.getByText("● Unsaved — active is still m-a."),
  ).toBeInTheDocument();

  await user.type(screen.getByLabelText("Request timeout"), "240");
  await user.click(screen.getByRole("button", { name: "Save model" }));
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenCalledWith({
      ai_review: { model: "m-b", request_timeout: 240 },
    }),
  );
  expect(
    await screen.findByText(/✓ Model saved — active: m-b · 240 s timeout/),
  ).toBeInTheDocument();
});

test("an out-of-range timeout is refused before any request", async () => {
  const user = userEvent.setup();
  renderSetup();
  await connected();
  await user.type(screen.getByLabelText("Request timeout"), "5");
  await user.click(screen.getByRole("button", { name: "Save model" }));
  expect(
    screen.getByText(/Request timeout must be 10–600 seconds/),
  ).toBeInTheDocument();
  expect(api.setPrefs).not.toHaveBeenCalled();
});

// ---- stale responses (#956) -------------------------------------------------------------

test("an edit while Save connection's check is pending saves nothing, and the form stays dirty", async () => {
  const user = userEvent.setup();
  const check = deferred<Listing>();
  renderSetup();
  await connected();
  vi.mocked(api.testAiEndpoint).mockReturnValue(check.promise);
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  await user.click(saveConnection());
  await user.type(baseUrl(), "x"); // the operator keeps typing while the check runs
  await act(async () => {
    check.resolve({ models: ["m-a"], listing: "ok" });
  });
  expect(api.setPrefs).not.toHaveBeenCalled();
  expect(screen.getByText(/● Unsaved/)).toBeInTheDocument();
  expect(screen.queryByText(/1 model available/)).toBeNull();
});

test("unmounting during a pending check saves nothing", async () => {
  const user = userEvent.setup();
  const check = deferred<Listing>();
  const { unmount } = renderSetup();
  await connected();
  vi.mocked(api.testAiEndpoint).mockReturnValue(check.promise);
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  await user.click(saveConnection());
  unmount();
  await act(async () => {
    check.resolve({ models: ["m-a"], listing: "ok" });
  });
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("a save the server already ACCEPTED is adopted, and a newer edit stays dirty", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  const { refresh } = renderSetup();
  await connected();
  vi.mocked(api.setPrefs).mockReturnValue(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled()); // the check passed; POST in flight

  await user.type(baseUrl(), "-next");
  await act(async () => {
    post.resolve(echo({ base_url: "https://ai.example.io/v2" }));
  });
  expect(refresh).toHaveBeenCalled();
  expect(baseUrl()).toHaveValue("https://ai.example.io/v2-next");
  expect(screen.getByText(/● Unsaved/)).toBeInTheDocument();
  // Undo the newer edit: the draft now equals what was SAVED, so nothing is unsaved.
  await user.type(baseUrl(), "{Backspace}{Backspace}{Backspace}{Backspace}{Backspace}");
  expect(screen.queryByText(/● Unsaved/)).toBeNull();
});

test("a key typed while its save is in flight survives that save (#836)", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  await connected();
  await user.click(screen.getByRole("button", { name: "Replace key" }));
  await user.type(apiKey(), "sk-one");
  vi.mocked(api.setPrefs).mockReturnValue(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  await user.type(apiKey(), "-more");
  await act(async () => {
    post.resolve(echo({ api_key: "sk-one" }));
  });
  expect(apiKey()).toHaveValue("sk-one-more");
});

// ---- write ownership and draft ownership (Hermes on #960) --------------------------------

test("first setup: a key typed while the first save is in flight stays visible, dirty and saveable", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup(UNCONFIGURED);
  await user.type(baseUrl(), "https://ai.example.io/v1");
  await user.type(apiKey(), "sk-a");
  vi.mocked(api.setPrefs).mockReturnValue(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  await user.clear(apiKey());
  await user.type(apiKey(), "sk-b"); // only A was submitted
  await act(async () => {
    post.resolve(
      echo(
        { base_url: "https://ai.example.io/v1", api_key: "sk-a" },
        UNCONFIGURED,
      ),
    );
  });
  // The accepted save flipped api_key_set, but the newer draft is still on screen and unsaved.
  expect(apiKey()).toHaveValue("sk-b");
  expect(screen.getByText(/● Unsaved/)).toBeInTheDocument();
  expect(saveConnection()).toBeEnabled();
});

test("host switch: a newer key typed while the save for the new host is in flight stays visible and dirty", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  await connected();
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://other.example.org/v1");
  await user.type(apiKey(), "sk-other");
  vi.mocked(api.setPrefs).mockReturnValue(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  await user.type(apiKey(), "-2");
  await act(async () => {
    post.resolve(
      echo({ base_url: "https://other.example.org/v1", api_key: "sk-other" }),
    );
  });
  expect(apiKey()).toHaveValue("sk-other-2");
  expect(screen.getByText(/● Unsaved/)).toBeInTheDocument();
});

test("no second write can start while one is in flight, so responses can never arrive reversed", async () => {
  const user = userEvent.setup();
  const first = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  const combo = await screen.findByRole("combobox", { name: "Model" });
  await user.selectOptions(combo, "m-b");
  vi.mocked(api.setPrefs).mockReturnValueOnce(
    first.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(screen.getByRole("button", { name: "Save model" }));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));

  // Editing the draft while A is pending must NOT hand out a second write.
  await user.type(screen.getByLabelText("Request timeout"), "240");
  expect(screen.getByRole("button", { name: "Save model" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove key" })).toBeDisabled();

  await act(async () => {
    first.resolve(echo({ model: "m-b", request_timeout: null }));
  });
  // A landed; the newer timeout is still a draft, and now it may be saved.
  expect(screen.getByLabelText("Request timeout")).toHaveValue(240);
  const save = screen.getByRole("button", { name: "Save model" });
  await waitFor(() => expect(save).toBeEnabled());
  await user.click(save);
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenLastCalledWith({
      ai_review: { model: "m-b", request_timeout: 240 },
    }),
  );
  expect(
    await screen.findByText(/✓ Model saved — active: m-b · 240 s timeout/),
  ).toBeInTheDocument();
});

test("a connection save in flight blocks another connection save even after an edit", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  await connected();
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  vi.mocked(api.setPrefs).mockReturnValue(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));
  await user.type(baseUrl(), "-next");
  expect(saveConnection()).toBeDisabled();
  await act(async () => {
    post.resolve(echo({ base_url: "https://ai.example.io/v2" }));
  });
  await waitFor(() => expect(saveConnection()).toBeEnabled());
  expect(api.setPrefs).toHaveBeenCalledTimes(1);
});

// ---- a newer edit back to the previous saved value (Hermes on #960) ---------------------

test("URL: an edit back to the previous saved URL while its save is in flight survives the accepted save", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  await connected();
  vi.mocked(api.setPrefs).mockReturnValueOnce(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v2");
  await user.click(saveConnection());
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));

  await user.clear(baseUrl());
  await user.type(baseUrl(), "https://ai.example.io/v1"); // back to what was saved before
  await act(async () => {
    post.resolve(echo({ base_url: "https://ai.example.io/v2" }));
  });
  expect(baseUrl()).toHaveValue("https://ai.example.io/v1");
  expect(screen.getByText(/● Unsaved/)).toBeInTheDocument();
  await waitFor(() => expect(saveConnection()).toBeEnabled());
  await user.click(saveConnection());
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenLastCalledWith({
      ai_review: { base_url: "https://ai.example.io/v1" },
    }),
  );
});

test("model: a pick back to the previous model while Save model is in flight survives the accepted save", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  const combo = await screen.findByRole("combobox", { name: "Model" });
  await user.selectOptions(combo, "m-b");
  vi.mocked(api.setPrefs).mockReturnValueOnce(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(screen.getByRole("button", { name: "Save model" }));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));

  await user.selectOptions(combo, "m-a"); // back to what was saved before
  await act(async () => {
    post.resolve(echo({ model: "m-b", request_timeout: null }));
  });
  expect(combo).toHaveValue("m-a");
  expect(
    screen.getByText("● Unsaved — active is still m-b."),
  ).toBeInTheDocument();
  const save = screen.getByRole("button", { name: "Save model" });
  await waitFor(() => expect(save).toBeEnabled());
  await user.click(save);
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenLastCalledWith({
      ai_review: { model: "m-a", request_timeout: null },
    }),
  );
});

test("timeout: clearing back to the server default while Save model is in flight survives the accepted save", async () => {
  const user = userEvent.setup();
  const post = deferred<{ ai_review: AiReviewConfig }>();
  renderSetup();
  await connected();
  const timeout = screen.getByLabelText("Request timeout");
  await user.type(timeout, "240");
  vi.mocked(api.setPrefs).mockReturnValueOnce(
    post.promise as unknown as Promise<Record<string, unknown>>,
  );
  await user.click(screen.getByRole("button", { name: "Save model" }));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(1));

  await user.clear(timeout); // back to the server default that was saved before
  await act(async () => {
    post.resolve(echo({ model: "m-a", request_timeout: 240 }));
  });
  expect(timeout).toHaveValue(null);
  expect(
    screen.getByText("● Unsaved — active is still m-a."),
  ).toBeInTheDocument();
  const save = screen.getByRole("button", { name: "Save model" });
  await waitFor(() => expect(save).toBeEnabled());
  await user.click(save);
  await waitFor(() =>
    expect(api.setPrefs).toHaveBeenLastCalledWith({
      ai_review: { model: "m-a", request_timeout: null },
    }),
  );
});
