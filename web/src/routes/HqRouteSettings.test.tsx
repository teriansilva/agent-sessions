/** Routing enablement keeps its save; model choices live in the shared HQ models group. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { HqRouteSettings } from "./HqRouteSettings";

vi.mock("../lib/api", () => ({ api: { setPrefs: vi.fn() } }));
function renderWith(config: Partial<AppConfig>) {
  render(<MemoryRouter><ConfigCtx.Provider value={config as AppConfig}><HqRouteSettings /></ConfigCtx.Provider></MemoryRouter>);
}
beforeEach(() => { vi.mocked(api.setPrefs).mockReset(); });

test("the routing default is enabled, and model choices have one home", () => {
  renderWith({});
  expect(screen.getByRole("checkbox", { name: /only when a question needs it/ })).toBeChecked();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "HQ models" })).toHaveAttribute("href", "/settings/ai-hq#hq-models-h");
});

test("switching routing off saves only enablement", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({ hq_route: { enabled: false, model: "router", thinking: "off" } });
  renderWith({ hq_route: { enabled: true, model: "router", thinking: "off" } });
  await userEvent.click(screen.getByRole("checkbox"));
  expect(api.setPrefs).toHaveBeenCalledWith({ hq_route: { enabled: false } });
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});

test("a refused save keeps the confirmed value", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("save refused"));
  renderWith({});
  await userEvent.click(screen.getByRole("checkbox"));
  expect(await screen.findByRole("alert")).toHaveTextContent("save refused");
  expect(screen.getByRole("checkbox")).toBeChecked();
});
