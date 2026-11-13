import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { PluginCatalog } from "../../types/plugins";
import { AgentCatalogStatus } from "./AgentCatalogStatus";

const feed = (automatic = true) => ({
  source: "signed",
  sequence: 4,
  refresh: { automatic, last_attempt: null, last_success: null, error: null },
}) as unknown as PluginCatalog["feed"];

test("the catalog card has no switches: no publisher toggle, no daily-check toggle (#1994)", () => {
  render(<AgentCatalogStatus feed={feed()} busy={false} refresh={vi.fn()} />);
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByText(/I publish these feeds/i)).toBeNull();
  expect(screen.queryByText(/Automatically check daily/i)).toBeNull();
  expect(screen.getByText(/installed agents update themselves when a newer signed version arrives/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh catalog" })).toBeInTheDocument();
});

test("while automatic updates are paused, the card says so and where to resume", () => {
  render(<AgentCatalogStatus feed={feed(false)} busy={false} refresh={vi.fn()} />);
  expect(screen.getByRole("status")).toHaveTextContent(/Automatic updates are paused.*Settings → Updates/);
});

test("on, there is no paused note", () => {
  render(<AgentCatalogStatus feed={feed(true)} busy={false} refresh={vi.fn()} />);
  expect(screen.queryByText(/Automatic updates are paused/)).toBeNull();
});

test("an expired remote catalog says a newer bundled version stays installable (#1861)", () => {
  render(<AgentCatalogStatus feed={{ ...feed(), stale: true }} busy={false} refresh={vi.fn()} />);
  expect(screen.getByText(/has expired/)).toHaveTextContent(/same recipe or a newer version/);
  expect(screen.queryByText(/Only unchanged recipes/)).toBeNull();
});
