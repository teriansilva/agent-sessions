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

import { api, ApiError } from "../lib/api";
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

test("a failed FIRST read is retried until one lands — not final for the page's lifetime", async () => {
  // The reload after Update now can reach a server that is still restarting (a 502 from the
  // proxy). That one failure used to leave the config null forever: no operator tile, so no way
  // into Settings, and no CSRF token.
  vi.mocked(api.config)
    .mockRejectedValueOnce(new Error("GET /api/config → 502"))
    .mockRejectedValueOnce(new Error("GET /api/config → 502"))
    .mockResolvedValue(cfg("up"));

  render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  expect(screen.getByTestId("version")).toHaveTextContent("none");
  // 1 s, then 2 s of backoff.
  await waitFor(
    () => expect(screen.getByTestId("version")).toHaveTextContent("up"),
    { timeout: 6_000 },
  );
  expect(api.config).toHaveBeenCalledTimes(3);
}, 10_000);

test("a failed refresh AFTER a successful read is not retried and keeps the config", async () => {
  vi.mocked(api.config)
    .mockResolvedValueOnce(cfg("first"))
    .mockRejectedValue(new Error("offline"));

  render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("version")).toHaveTextContent("first"),
  );
  await userEvent.click(screen.getByText("refresh"));
  await new Promise((r) => setTimeout(r, 1_500));
  expect(api.config).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("version")).toHaveTextContent("first");
});

test("a first read that rejects AFTER unmount schedules nothing", async () => {
  // Home Free's teardown unmounts the app and then disposes the tunnel under it, so a pending
  // first read can reject after cleanup. It must not start a retry loop from a dead provider.
  let reject: (e: unknown) => void = () => {};
  vi.mocked(api.config).mockImplementation(
    () =>
      new Promise<AppConfig>((_, rej) => {
        reject = rej;
      }),
  );
  const { unmount } = render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(1));
  unmount();
  reject(new Error("tunnel closed"));
  await new Promise((r) => setTimeout(r, 3_100));
  expect(api.config).toHaveBeenCalledTimes(1);
}, 10_000);

test("unmount cancels an already scheduled retry", async () => {
  vi.mocked(api.config).mockRejectedValue(new Error("GET /api/config → 502"));
  const { unmount } = render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(1));
  // Let the rejection land and the 1 s retry be scheduled, then unmount before it fires.
  await new Promise((r) => setTimeout(r, 100));
  unmount();
  await new Promise((r) => setTimeout(r, 3_100));
  expect(api.config).toHaveBeenCalledTimes(1);
}, 10_000);

test("an auth refusal is not retried", async () => {
  vi.mocked(api.config).mockRejectedValue(new ApiError(403, "forbidden"));
  render(
    <ConfigProvider>
      <Probe />
    </ConfigProvider>,
  );
  await waitFor(() => expect(api.config).toHaveBeenCalledTimes(1));
  await new Promise((r) => setTimeout(r, 1_500));
  expect(api.config).toHaveBeenCalledTimes(1);
});
