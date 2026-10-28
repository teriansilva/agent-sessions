import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import type { PluginCatalog } from "../../types/plugins";
import { AgentCatalogStatus } from "./AgentCatalogStatus";

const feed = (publisher_alerts?: boolean) => ({
  source: "signed",
  sequence: 4,
  refresh: { automatic: true, last_attempt: null, last_success: null, error: null, publisher_alerts },
}) as unknown as PluginCatalog["feed"];

const label = /alert me before the agent catalog or the gallery lapses/i;

test("the publisher alerts toggle shows the stored choice and reports a change (#1730)", async () => {
  const configureAlerts = vi.fn();
  render(<AgentCatalogStatus feed={feed(false)} busy={false} refresh={vi.fn()} configure={vi.fn()} configureAlerts={configureAlerts} />);
  const box = screen.getByRole("checkbox", { name: label });
  expect(box).not.toBeChecked();
  await userEvent.click(box);
  expect(configureAlerts).toHaveBeenCalledWith(true);
});

test("an enabled alert setting renders checked", () => {
  render(<AgentCatalogStatus feed={feed(true)} busy={false} refresh={vi.fn()} configure={vi.fn()} configureAlerts={vi.fn()} />);
  expect(screen.getByRole("checkbox", { name: label })).toBeChecked();
});

test("setup, which passes no alerts handler, shows no alerts toggle", () => {
  render(<AgentCatalogStatus feed={feed(false)} busy={false} refresh={vi.fn()} configure={vi.fn()} />);
  expect(screen.queryByRole("checkbox", { name: label })).toBeNull();
  expect(screen.getByRole("checkbox", { name: /automatically check daily/i })).toBeInTheDocument();
});
