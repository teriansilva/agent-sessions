/** The config provider's response ORDERING (#900 review 5, finding 8).
 *
 *  Every response used to be applied in whatever order it arrived, and two saves with two
 *  refreshes is not an exotic sequence — each save triggers one. An older response landing last
 *  rolled the whole config back to the state before the newer save, and in the playbook editor
 *  that is visible and expensive: the panel re-seeds from config when it is not dirty, the newer
 *  save had just cleared `dirty`, and the next save then persisted the block the operator had
 *  already replaced.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../lib/api";
import type { AppConfig } from "../types/api";

import { useConfig, useConfigRefresh } from "./config";
import { ConfigProvider } from "./ConfigContext";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: { config: vi.fn() },
    setCsrfToken: vi.fn(),
    gotoChangePassword: vi.fn(),
  };
});

function cfg(version: string): AppConfig {
  return { version, csrf: "t", must_change_password: false } as AppConfig;
}

function Probe() {
  const config = useConfig();
  const refresh = useConfigRefresh();
  return (
    <>
      <div data-testid="version">{config?.version ?? "none"}</div>
      <button type="button" onClick={() => refresh()}>
        refresh
      </button>
    </>
  );
}

beforeEach(() => {
  vi.mocked(api.config).mockReset();
});

test("an OLDER config response cannot roll back a newer one", async () => {
  // Two reads in flight; the FIRST resolves last. Without a ticket the provider applies it and
  // the app reverts to a state the operator has already replaced.
  const releases: ((c: AppConfig) => void)[] = [];
  vi.mocked(api.config).mockImplementation(
    () =>
      new Promise<AppConfig>((res) => {
        releases.push(res);
      }),
  );

  render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() => expect(releases.length).toBe(1));

  // A second read is issued (the mount's is still in flight).
  await userEvent.click(screen.getByText("refresh"));
  await waitFor(() => expect(releases.length).toBe(2));

  // THE NEWER ONE LANDS FIRST …
  releases[1](cfg("new"));
  await waitFor(() =>
    expect(screen.getByTestId("version")).toHaveTextContent("new"),
  );

  // … and the older one lands after it, and changes nothing.
  releases[0](cfg("old"));
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.getByTestId("version")).toHaveTextContent("new");
});

test("a FAILED newer read does not let an older one roll the config back", async () => {
  // #900 review 7, finding 8. The guard compared against the latest APPLIED ticket, which answers
  // a different question — "is this the newest response so far". So: A is issued, a save happens,
  // B is issued and FAILS, and A (carrying pre-save values) resolves afterwards and is applied.
  //
  // Red against `tk <= applied.current`.
  const resolvers: ((c: AppConfig) => void)[] = [];
  const rejecters: ((e: Error) => void)[] = [];
  vi.mocked(api.config).mockImplementation(
    () =>
      new Promise<AppConfig>((res, rej) => {
        resolvers.push(res);
        rejecters.push(rej);
      }),
  );

  render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() => expect(resolvers.length).toBe(1));

  // The mount read (A) is still in flight when the save's refresh (B) is issued.
  await userEvent.click(screen.getByText("refresh"));
  await waitFor(() => expect(resolvers.length).toBe(2));

  // B FAILS — the network dropped, the server 500'd, it does not matter which.
  rejecters[1](new Error("boom"));
  await new Promise((r) => setTimeout(r, 20));

  // …and A, which describes a moment we have already asked to move past, resolves after it.
  resolvers[0](cfg("stale"));
  await new Promise((r) => setTimeout(r, 20));
  expect(screen.getByTestId("version")).toHaveTextContent("none");

  // A later read is applied normally, which is what resolves the degraded state.
  await userEvent.click(screen.getByText("refresh"));
  await waitFor(() => expect(resolvers.length).toBe(3));
  resolvers[2](cfg("fresh"));
  await waitFor(() =>
    expect(screen.getByTestId("version")).toHaveTextContent("fresh"),
  );
});
