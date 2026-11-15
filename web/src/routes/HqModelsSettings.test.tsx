import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import type { VoiceSettings } from "../types/voice";
import { HqModelsSettings } from "./HqModelsSettings";

vi.mock("../lib/api", () => ({ api: {
  config: vi.fn(), voiceSettings: vi.fn(), aiReviewModels: vi.fn(),
  setPrefs: vi.fn(), saveVoiceSettings: vi.fn(),
} }));
let config: AppConfig;
let call: VoiceSettings;
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
beforeEach(() => {
  vi.resetAllMocks();
  config = {
    ai_review: { model: "endpoint", thinking: "medium" },
    hq_models: { typed: { model: "", thinking: "hq" }, mission: { model: "", thinking: "hq" } },
    hq_route: { enabled: true, model: "", thinking: "off" },
  } as AppConfig;
  call = { hq_model: "", thinking: "off" } as VoiceSettings;
  vi.mocked(api.config).mockImplementation(async () => config);
  vi.mocked(api.voiceSettings).mockImplementation(async () => call);
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: ["endpoint", "fast", "hq"] });
  vi.mocked(api.setPrefs).mockImplementation(async (patch) => {
    const p = patch as Partial<AppConfig>;
    if (p.hq_route) config = { ...config, hq_route: { ...config.hq_route!, ...p.hq_route } };
    if (p.hq_models) config = { ...config, hq_models: {
      typed: { ...config.hq_models!.typed, ...p.hq_models.typed },
      mission: { ...config.hq_models!.mission, ...p.hq_models.mission },
    } };
    return config as unknown as Record<string, unknown>;
  });
  vi.mocked(api.saveVoiceSettings).mockImplementation(async (patch) => {
    call = { ...call, ...patch } as VoiceSettings;
    return call;
  });
});

async function ready() {
  render(<HqModelsSettings />);
  await waitFor(() => expect(screen.getByLabelText("Model for typed turns")).toBeEnabled());
}

test("defaults distinguish Inherit from provider Auto and name each field's source", async () => {
  await ready();
  for (const [name, model, thinking] of [
    ["typed turns", "", "hq"], ["question routing", "", "off"],
    ["calls", "", "off"], ["mission conversations", "", "hq"],
  ]) {
    expect(screen.getByLabelText(`Model for ${name}`)).toHaveValue(model);
    const field = screen.getByLabelText(`Thinking for ${name}`);
    expect(field).toHaveValue(thinking);
    expect(within(field).getByRole("option", { name: "Inherit" })).toHaveValue("hq");
    expect(within(field).getByRole("option", { name: "Auto · provider policy" })).toHaveValue("auto");
  }
  expect(screen.getByRole("group", { name: "Question routing" })).toHaveTextContent("enclosing call");
  expect(screen.getByRole("group", { name: "Typed turns" })).toHaveTextContent("From Endpoint & model · Medium");
});

test.each([
  ["typed turns", { hq_models: { typed: { model: "fast" } } }],
  ["question routing", { hq_route: { model: "fast" } }],
  ["mission conversations", { hq_models: { mission: { model: "fast" } } }],
])("%s saves only its model and keeps the accepted value", async (row, patch) => {
  await ready();
  const field = screen.getByLabelText(`Model for ${row}`);
  await userEvent.selectOptions(field, "fast");
  expect(api.setPrefs).toHaveBeenCalledExactlyOnceWith(patch);
  await waitFor(() => expect(field).toHaveValue("fast"));
});

test("calls keep their canonical keys, including Auto, and never send speech fields", async () => {
  await ready();
  await userEvent.selectOptions(screen.getByLabelText("Thinking for calls"), "auto");
  await waitFor(() => expect(screen.getByLabelText("Thinking for calls")).toHaveValue("auto"));
  await userEvent.selectOptions(screen.getByLabelText("Model for calls"), "fast");
  expect(api.saveVoiceSettings).toHaveBeenNthCalledWith(1, { thinking: "auto" });
  expect(api.saveVoiceSettings).toHaveBeenNthCalledWith(2, { hq_model: "fast" });
  expect(api.setPrefs).not.toHaveBeenCalled();
});

test("a saved unlisted model survives a failed catalog; retry adds the catalog without replacing it", async () => {
  config.hq_models!.typed.model = "retired";
  vi.mocked(api.aiReviewModels).mockRejectedValueOnce(new Error("catalog offline"));
  await ready();
  const field = screen.getByLabelText("Model for typed turns");
  expect(field).toHaveValue("retired");
  expect(within(field).getAllByRole("option")).toHaveLength(2);
  expect(screen.getByText(/Model list unavailable/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Reload model list" }));
  await waitFor(() => expect(within(field).getByRole("option", { name: "fast" })).toBeInTheDocument());
  expect(field).toHaveValue("retired");
});

test("malformed catalog and rejected save preserve the confirmed choices", async () => {
  call.hq_model = "saved";
  vi.mocked(api.aiReviewModels).mockResolvedValue({} as never);
  vi.mocked(api.saveVoiceSettings).mockRejectedValue(new Error("save refused"));
  await ready();
  expect(screen.getByLabelText("Model for calls")).toHaveValue("saved");
  await userEvent.selectOptions(screen.getByLabelText("Thinking for calls"), "high");
  expect(await screen.findByRole("alert")).toHaveTextContent("save refused");
  expect(screen.getByLabelText("Thinking for calls")).toHaveValue("off");
  await userEvent.click(screen.getByRole("button", { name: "Reload settings" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

test("unreadable settings disable only missing rows until an explicit reload succeeds", async () => {
  call.thinking = "hq";
  vi.mocked(api.config).mockRejectedValueOnce(new Error("offline"));
  render(<HqModelsSettings />);
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be read");
  expect(screen.getByLabelText("Model for typed turns")).toBeDisabled();
  expect(screen.getByLabelText("Model for calls")).toBeEnabled();
  expect(screen.getByRole("group", { name: "Calls" })).toHaveTextContent("unavailable until settings reload");
  await userEvent.click(screen.getByRole("button", { name: "Reload settings" }));
  await waitFor(() => expect(screen.getByLabelText("Model for typed turns")).toBeEnabled());
  expect(screen.getByRole("group", { name: "Calls" })).toHaveTextContent("From Endpoint & model · Medium");
});

test("each row serializes its own writes without a late response replacing another row", async () => {
  const first = deferred<Record<string, unknown>>();
  vi.mocked(api.setPrefs).mockReturnValueOnce(first.promise);
  await ready();
  const typed = screen.getByLabelText("Model for typed turns");
  await userEvent.selectOptions(typed, "fast");
  expect(typed).toBeDisabled();
  expect(typed).toHaveValue("");
  await userEvent.selectOptions(screen.getByLabelText("Thinking for mission conversations"), "low");
  await waitFor(() => expect(screen.getByLabelText("Thinking for mission conversations")).toHaveValue("low"));
  await act(async () => first.resolve({ hq_models: {
    typed: { model: "fast", thinking: "hq" }, mission: { model: "", thinking: "hq" },
  } }));
  expect(typed).toHaveValue("fast");
  expect(screen.getByLabelText("Thinking for mission conversations")).toHaveValue("low");
});

test("a model literally named hq remains an explicit choice, including as a call fallback", async () => {
  call.hq_model = "hq";
  config.hq_models!.typed.model = "hq";
  await ready();
  expect(screen.getByRole("group", { name: "Typed turns" })).toHaveTextContent("Own choice · hq");
  expect(screen.getByRole("group", { name: "Question routing" })).toHaveTextContent("enclosing call (hq)");
});

test("Strict Mode's superseded settings read cannot overwrite the current one", async () => {
  const old = deferred<AppConfig>();
  vi.mocked(api.config).mockReturnValueOnce(old.promise);
  config.hq_models!.typed.model = "fast";
  render(<StrictMode><HqModelsSettings /></StrictMode>);
  await waitFor(() => expect(screen.getByLabelText("Model for typed turns")).toHaveValue("fast"));
  await act(async () => old.resolve({ ...config, hq_models: {
    ...config.hq_models!, typed: { model: "stale", thinking: "hq" },
  } }));
  expect(screen.getByLabelText("Model for typed turns")).toHaveValue("fast");
});
