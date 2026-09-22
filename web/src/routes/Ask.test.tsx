/** The `/ask` page (#1058) — the page around the console, which is what this route adds.
 *
 *  `AskConsole.test.tsx` owns the box's behaviour. What is asserted here is the thing the route
 *  exists for: that a missing AI endpoint is EXPLAINED and not merely disabled. A greyed-out field
 *  with "Needs an AI endpoint" in it is a symptom; the operator needs to know where to go.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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
  return { ...actual, api: { pulseAsk: vi.fn() } };
});

import Ask from "./Ask";

function mount(cfg: Partial<AppConfig> | undefined) {
  config.value = cfg as AppConfig | undefined;
  return render(
    <MemoryRouter>
      <Ask />
    </MemoryRouter>,
  );
}

test("a configured endpoint gives a usable box and no warning", () => {
  mount({ pulse: { configured: true } } as Partial<AppConfig>);
  expect(screen.getByTestId("ask-page")).toBeInTheDocument();
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
