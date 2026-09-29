/** The dashboard (#1123) around its Ask field — and, since #1171, where asking on it goes.
 *
 *  `AskConsole.test.tsx` owns the conversation's behaviour. What is asserted here: a missing AI
 *  endpoint is EXPLAINED and not merely disabled (a greyed-out field is a symptom; the operator
 *  needs to know where to go), and a question asked here opens Ask's own page and is asked THERE,
 *  once.
 */
import { cleanup, render, screen } from "@testing-library/react";
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

import { api } from "../lib/api";
import { ASK_PATH, DASHBOARD_PATH, NEW_PROJECT_PATH } from "../lib/routes";
import Ask from "./Ask";
import Dashboard from "./Dashboard";

/** Where the router is, and what the entry carries — the hand-off is router state. */
function Where() {
  const loc = useLocation();
  return (
    <div data-testid="where" data-state={JSON.stringify(loc.state ?? null)}>
      {loc.pathname}
    </div>
  );
}

function mount(cfg: Partial<AppConfig> | undefined) {
  config.value = cfg as AppConfig | undefined;
  return render(
    <MemoryRouter initialEntries={[DASHBOARD_PATH]}>
      <Routes>
        <Route path={DASHBOARD_PATH} element={<Dashboard />} />
        <Route path={ASK_PATH} element={<Ask />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

test("a configured endpoint gives a usable box and no warning", () => {
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  expect(screen.getByTestId("dashboard-page")).toBeInTheDocument();
  expect(screen.getByTestId("composer-input")).toBeEnabled();
  expect(screen.queryByTestId("ask-needs-endpoint")).toBeNull();
});

test("no endpoint SAYS SO and links to the page that fixes it", () => {
  mount({ pulse: { configured: false } } as Partial<AppConfig>);
  const notice = screen.getByTestId("ask-needs-endpoint");
  expect(notice).toHaveTextContent(/needs an ai endpoint/i);
  expect(
    screen.getByRole("link", { name: /endpoint & model/i }),
  ).toHaveAttribute("href", "/settings/ai-endpoint");
  expect(screen.getByTestId("composer-input")).toBeDisabled();
});

test("a config that has not arrived is treated as NOT configured, not as configured", () => {
  // Fail closed on the explanation: showing an enabled box that 409s is worse than showing the
  // notice for the moment before the config lands. `pulse` is also absent on an older server.
  mount(undefined);
  expect(screen.getByTestId("ask-needs-endpoint")).toBeInTheDocument();
  cleanup();
  // …and so is a server old enough to have no `pulse` block at all.
  mount({} as Partial<AppConfig>);
  expect(screen.getByTestId("ask-needs-endpoint")).toBeInTheDocument();
});

test("asking on the dashboard opens Ask's page and asks it there, once (#1171)", async () => {
  vi.mocked(api.pulseAskStream).mockImplementation(async (_q, _h, onEvent) => {
    onEvent({
      type: "answer",
      final: true,
      answer: "Handed over.",
      matches: [],
      stage: "catalog",
      configured: true,
    });
  });
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  await userEvent.type(screen.getByTestId("composer-input"), "which session?{Enter}");
  expect(await screen.findByTestId("ask-page")).toBeInTheDocument();
  expect(screen.getByTestId("where")).toHaveTextContent(ASK_PATH);
  expect(await screen.findByText("Handed over.")).toBeInTheDocument();
  expect(api.pulseAskStream).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.pulseAskStream).mock.calls[0][0]).toBe("which session?");
  // The entry no longer carries the question: a reload or Back/Forward cannot ask it again.
  expect(screen.getByTestId("where")).toHaveAttribute("data-state", "null");
  // …and the dashboard's tiles did not come along.
  expect(screen.queryByTestId("dashboard-page")).toBeNull();
});

test("New project opens the wizard, keyed to come back here (#1187)", async () => {
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  await userEvent.click(screen.getByRole("link", { name: /new project/i }));
  expect(screen.getByTestId("where")).toHaveTextContent(NEW_PROJECT_PATH);
  expect(screen.getByTestId("where")).toHaveAttribute(
    "data-state",
    JSON.stringify({ from: "dashboard" }),
  );
});
