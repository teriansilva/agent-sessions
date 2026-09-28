import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ConfigRefreshCtx } from "../../app/config";
import { ApiError, api } from "../../lib/api";
import type { AgentEndpoint } from "../../types/api";
import { AgentEndpointCard } from "./AgentEndpointCard";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      agentEndpoint: vi.fn(),
      setAgentEndpoint: vi.fn(),
      testAgentEndpoint: vi.fn(),
      engines: vi.fn(),
    },
  };
});

const STORED: AgentEndpoint = {
  base_url: "https://llm.example.lan/v1",
  model: "qwen3-coder",
  api_key_set: true,
  context_window: 32768,
  max_output_tokens: 4096,
  request_timeout: null,
  configured: true,
};

const refresh = vi.fn();

function renderCard() {
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <AgentEndpointCard engine="apichat" />
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.agentEndpoint).mockResolvedValue(STORED);
  vi.mocked(api.engines).mockResolvedValue({ engines: [], problems: [] });
});

afterEach(() => vi.clearAllMocks());

test("the stored key is never shown; the field says one is stored", async () => {
  renderCard();
  const key = await screen.findByLabelText("API key");
  expect(key).toHaveValue("");
  expect(key).toHaveAttribute("placeholder", expect.stringMatching(/stored, encrypted/));
  expect(screen.getByLabelText("Base URL")).toHaveValue(STORED.base_url);
  expect(screen.getByText(/Tools:/)).toHaveTextContent(/cannot run commands or edit files/);
});

test("TEST checks the draft and saves nothing — and sends no key unless one was typed", async () => {
  vi.mocked(api.testAgentEndpoint).mockResolvedValue({ models: ["a", "b"], listing: "ok" });
  renderCard();
  await screen.findByDisplayValue(STORED.base_url);
  await userEvent.click(screen.getByRole("button", { name: "Test" }));
  expect(api.testAgentEndpoint).toHaveBeenCalledWith("apichat", { base_url: STORED.base_url });
  expect(api.setAgentEndpoint).not.toHaveBeenCalled();
  expect(await screen.findByTestId("endpoint-status")).toHaveTextContent(/2 models.*Nothing was saved/);
});

test("SAVE sends only what changed, then refreshes the roster and config", async () => {
  vi.mocked(api.setAgentEndpoint).mockResolvedValue({ ...STORED, model: "other" });
  renderCard();
  const model = await screen.findByDisplayValue("qwen3-coder");
  await userEvent.clear(model);
  await userEvent.type(model, "other");
  await userEvent.type(screen.getByLabelText("API key"), "sk-new");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(api.setAgentEndpoint).toHaveBeenCalledWith("apichat", { model: "other", api_key: "sk-new" });
  expect(await screen.findByTestId("endpoint-status")).toHaveTextContent(/available now/);
  expect(api.engines).toHaveBeenCalled();
  expect(refresh).toHaveBeenCalled();
  expect(screen.getByLabelText("API key")).toHaveValue(""); // the typed key does not linger
});

test("Remove key clears it explicitly", async () => {
  vi.mocked(api.setAgentEndpoint).mockResolvedValue({ ...STORED, api_key_set: false, configured: false });
  renderCard();
  await userEvent.click(await screen.findByRole("button", { name: "Remove key" }));
  expect(api.setAgentEndpoint).toHaveBeenCalledWith("apichat", { api_key: null });
});

test("a refusal from the origin policy is shown as it was given", async () => {
  vi.mocked(api.setAgentEndpoint).mockRejectedValue(
    new ApiError(422, "enter the API key for https://other.example:443 — the stored key is only sent to the endpoint it was saved for"),
  );
  renderCard();
  const url = await screen.findByLabelText("Base URL");
  await userEvent.clear(url);
  await userEvent.type(url, "https://other.example/v1");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/only sent to the endpoint it was saved for/);
});

test("the fields are read-only while a save is in flight, so its result cannot erase newer edits (Hermes on #1219)", async () => {
  let resolve!: (v: AgentEndpoint) => void;
  vi.mocked(api.setAgentEndpoint).mockReturnValue(
    new Promise((r) => {
      resolve = r;
    }),
  );
  renderCard();
  const model = await screen.findByDisplayValue("qwen3-coder");
  await userEvent.clear(model);
  await userEvent.type(model, "first");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  for (const label of ["Base URL", "API key", "Model"]) {
    expect(screen.getByLabelText(label)).toHaveAttribute("readonly");
  }
  await userEvent.type(model, "-later");
  expect(model).toHaveValue("first");
  resolve({ ...STORED, model: "first" });
  await waitFor(() => expect(model).not.toHaveAttribute("readonly"));
});
