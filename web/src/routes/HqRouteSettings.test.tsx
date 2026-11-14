/** Settings → AI → HQ → Question routing (#2028): on by default, saved on change, a model list
 *  from the endpoint, and "Same as HQ" for the model and the thinking level. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { HqRouteSettings } from "./HqRouteSettings";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { setPrefs: vi.fn(), aiReviewModels: vi.fn() } };
});

function renderWith(config: Partial<AppConfig>) {
  render(
    <ConfigCtx.Provider value={config as AppConfig}>
      <HqRouteSettings />
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset();
  vi.mocked(api.aiReviewModels).mockReset();
  vi.mocked(api.aiReviewModels).mockResolvedValue({ models: ["fast-1", "big-2"] } as never);
});

test("an older server's config reads as the defaults: on, HQ's model, thinking off", async () => {
  renderWith({});
  expect(screen.getByRole("checkbox", { name: /only when a question needs it/ })).toBeChecked();
  expect(screen.getByLabelText("Model for this decision")).toHaveValue("");
  expect(screen.getByLabelText("Thinking for this decision")).toHaveValue("off");
  expect(await screen.findByRole("option", { name: "fast-1" })).toBeInTheDocument();
});

test("switching it off saves only that field, and greys the model and thinking", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({
    hq_route: { enabled: false, model: "", thinking: "off" },
  });
  renderWith({ hq_route: { enabled: true, model: "", thinking: "off" } });
  await userEvent.click(screen.getByRole("checkbox", { name: /only when a question needs it/ }));
  expect(api.setPrefs).toHaveBeenCalledWith({ hq_route: { enabled: false } });
  expect(screen.getByRole("checkbox", { name: /only when a question needs it/ })).not.toBeChecked();
  expect(screen.getByLabelText("Model for this decision")).toBeDisabled();
  expect(screen.getByLabelText("Thinking for this decision")).toBeDisabled();
});

test("a model and a thinking level are saved as picked", async () => {
  vi.mocked(api.setPrefs)
    .mockResolvedValueOnce({ hq_route: { enabled: true, model: "fast-1", thinking: "off" } })
    .mockResolvedValueOnce({ hq_route: { enabled: true, model: "fast-1", thinking: "hq" } });
  renderWith({ hq_route: { enabled: true, model: "", thinking: "off" } });
  await screen.findByRole("option", { name: "fast-1" });
  await userEvent.selectOptions(screen.getByLabelText("Model for this decision"), "fast-1");
  expect(api.setPrefs).toHaveBeenLastCalledWith({ hq_route: { model: "fast-1" } });
  await userEvent.selectOptions(screen.getByLabelText("Thinking for this decision"), "hq");
  expect(api.setPrefs).toHaveBeenLastCalledWith({ hq_route: { thinking: "hq" } });
  expect(screen.getByLabelText("Thinking for this decision")).toHaveValue("hq");
});

test("a saved model the endpoint no longer lists stays choosable", async () => {
  renderWith({ hq_route: { enabled: true, model: "retired-9", thinking: "low" } });
  expect(screen.getByLabelText("Model for this decision")).toHaveValue("retired-9");
  expect(screen.getByRole("option", { name: "retired-9" })).toBeInTheDocument();
});

test("a refused save says so", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("hq_route.thinking must be one of …"));
  renderWith({});
  await userEvent.selectOptions(screen.getByLabelText("Thinking for this decision"), "high");
  expect(await screen.findByRole("alert")).toHaveTextContent("hq_route.thinking");
});
