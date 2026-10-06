/** The dashboard (#1123) — its head row since #1294.
 *
 *  Ask is the right-hand sidebar now (`AskSidebar`), so what is asserted here is what the dashboard
 *  holds of it: NOTHING but a button that opens the sidebar — no docked field, no "needs an AI
 *  endpoint" notice (the sidebar says that itself). And New is one menu with the three ways to start
 *  work: a session, a mission, a project (keyed to come back here, #1187).
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { expect, test, vi } from "vitest";

import type { AppConfig } from "../types/api";

const config = vi.hoisted(() => ({ value: undefined as AppConfig | undefined }));

vi.mock("../app/config", () => ({
  useConfig: () => config.value,
  useConfigRefresh: () => () => {},
}));

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { pulseAskStream: vi.fn() } };
});

import { AskPanelContext } from "../components/ask/askPanel";
import { api } from "../lib/api";
import {
  DASHBOARD_PATH,
  MISSION_PATH,
  NEW_PROJECT_PATH,
  SESSIONS_PATH,
} from "../lib/routes";
import Dashboard from "./Dashboard";

/** Where the router is, and what the entry carries. */
function Where() {
  const loc = useLocation();
  return (
    <div data-testid="where" data-state={JSON.stringify(loc.state ?? null)}>
      {loc.pathname}
    </div>
  );
}

function mount(cfg: Partial<AppConfig> | undefined, openAsk = vi.fn()) {
  config.value = cfg as AppConfig | undefined;
  render(
    <AskPanelContext.Provider
      value={{
        open: false,
        openAsk,
        close: () => {},
        toggle: () => {},
        triggerRef: { current: null },
      }}
    >
      <MemoryRouter initialEntries={[DASHBOARD_PATH]}>
        <Routes>
          <Route path="*" element={<Dashboard />} />
        </Routes>
        <Where />
      </MemoryRouter>
    </AskPanelContext.Provider>,
  );
  return openAsk;
}

test.each([
  ["configured", { pulse: { configured: true } }],
  ["unconfigured", { pulse: { configured: false } }],
  ["config not arrived", undefined],
])("no Ask field and no endpoint notice on the dashboard (%s) (#1294)", (_n, cfg) => {
  mount(cfg as Partial<AppConfig> | undefined);
  expect(screen.getByTestId("dashboard-page")).toBeInTheDocument();
  expect(screen.queryByTestId("composer-input")).toBeNull();
  expect(screen.queryByTestId("ask-needs-endpoint")).toBeNull();
});

test("the Ask button opens the sidebar and asks nothing itself (#1294)", async () => {
  const openAsk = mount({ pulse: { configured: true } } as Partial<AppConfig>);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
  expect(openAsk).toHaveBeenCalledTimes(1);
  // It stays on the dashboard: Ask is not a page.
  expect(screen.getByTestId("where")).toHaveTextContent(DASHBOARD_PATH);
  expect(api.pulseAskStream).not.toHaveBeenCalled();
});

test("New is a menu: a session, a mission, a project (#1294)", async () => {
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  await userEvent.click(screen.getByRole("button", { name: "New" }));
  const menu = await screen.findByRole("menu", { name: "New" });
  expect(
    within(menu)
      .getAllByRole("menuitem")
      .map((i) => [i.textContent?.trim(), i.getAttribute("href")]),
  ).toEqual([
    ["New session", SESSIONS_PATH],
    ["New mission", MISSION_PATH],
    ["New project", NEW_PROJECT_PATH],
  ]);
});

test("New → New project opens the wizard, keyed to come back here (#1187)", async () => {
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  await userEvent.click(screen.getByRole("button", { name: "New" }));
  await userEvent.click(
    await screen.findByRole("menuitem", { name: /new project/i }),
  );
  expect(screen.getByTestId("where")).toHaveTextContent(NEW_PROJECT_PATH);
  expect(screen.getByTestId("where")).toHaveAttribute(
    "data-state",
    JSON.stringify({ from: "dashboard" }),
  );
  expect(screen.queryByRole("menu")).toBeNull();
});
