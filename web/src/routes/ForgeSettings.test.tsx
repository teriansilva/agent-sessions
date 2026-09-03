/** The forge connection panel (#891).
 *
 *  Two properties, and both are about the credential:
 *
 *  1. **The token is write-only.** It never comes back from the server, so the panel must never
 *     round-trip a value it does not have — sending an empty or masked token would either erase a
 *     working credential or pretend to save one.
 *  2. **Clearing is explicit.** `null` clears where `""` preserves; an operator who wants the
 *     credential gone should not have to guess which empty value means which.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../lib/api";

import { ForgeSettings } from "./ForgeSettings";

const refresh = vi.fn();
let cfg: unknown = null;

vi.mock("../app/config", () => ({
  useConfig: () => cfg,
  useConfigRefresh: () => refresh,
}));

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

function forge(over: Record<string, unknown> = {}) {
  return {
    enabled: true,
    kind: "forgejo",
    base_url: "https://git.example",
    owner: "acme",
    token_set: false,
    configured: true,
    ...over,
  };
}

beforeEach(() => {
  refresh.mockReset();
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({} as never);
  cfg = { forge: forge() };
});

test("saving does NOT send a token the operator did not type", async () => {
  cfg = { forge: forge({ token_set: true }) };
  render(<ForgeSettings />);
  await userEvent.clear(screen.getByTestId("forge-owner"));
  await userEvent.type(screen.getByTestId("forge-owner"), "widgets");
  await userEvent.click(screen.getByTestId("forge-save"));

  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const patch = (vi.mocked(api.setPrefs).mock.calls[0][0] as { forge: Record<string, unknown> })
    .forge;
  expect(patch.owner).toBe("widgets");
  // Not "" and not a mask — ABSENT. The route preserves what is stored, and a request with no
  // reason to carry a credential does not carry one.
  expect("token" in patch).toBe(false);
});

test("a typed token IS sent, once", async () => {
  render(<ForgeSettings />);
  await userEvent.click(screen.getByTestId("forge-token-edit"));
  await userEvent.type(screen.getByTestId("forge-token"), "abc123");
  await userEvent.click(screen.getByTestId("forge-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const patch = (vi.mocked(api.setPrefs).mock.calls[0][0] as { forge: Record<string, unknown> })
    .forge;
  expect(patch.token).toBe("abc123");
});

test("clearing sends null, which is what the route treats as 'remove it'", async () => {
  cfg = { forge: forge({ token_set: true }) };
  render(<ForgeSettings />);
  await userEvent.click(screen.getByTestId("forge-token-clear"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  expect(vi.mocked(api.setPrefs).mock.calls[0][0]).toEqual({ forge: { token: null } });
});

test("a save refreshes the config, so `token_set` is not stale on the next render", async () => {
  render(<ForgeSettings />);
  await userEvent.click(screen.getByTestId("forge-save"));
  // The "my settings don't save" shape: a panel that writes and keeps rendering the config it
  // mounted with looks like the write was ignored.
  await waitFor(() => expect(refresh).toHaveBeenCalled());
});

test("an unconfigured forge says objectives read UNKNOWN, not failed", () => {
  cfg = { forge: forge({ enabled: false, base_url: "", configured: false }) };
  render(<ForgeSettings />);
  const note = screen.getByTestId("forge-unconfigured");
  expect(note).toHaveTextContent(/unknown/i);
  // The distinction is the whole three-way answer, and saying "failed" here would undo it.
  expect(note).toHaveTextContent(/has not been shown to be unmet/i);
});

test("the panel adopts the server's values once the config arrives", async () => {
  cfg = null;
  const { rerender } = render(<ForgeSettings />);
  expect(screen.getByTestId("forge-base-url")).toHaveValue("");
  cfg = { forge: forge({ base_url: "https://git.acme", owner: "acme" }) };
  rerender(<ForgeSettings />);
  await waitFor(() =>
    expect(screen.getByTestId("forge-base-url")).toHaveValue("https://git.acme"),
  );
});

test("a token typed WHILE a save is in flight is not wiped by that save", async () => {
  // #897 review: the field stays editable during the request, and the success path used to clear
  // it unconditionally — so token B, typed after A was submitted, disappeared unsaved.
  let release: ((v: unknown) => void) | undefined;
  vi.mocked(api.setPrefs).mockReturnValue(
    new Promise((r) => (release = r)) as never,
  );
  render(<ForgeSettings />);
  await userEvent.click(screen.getByTestId("forge-token-edit"));
  await userEvent.type(screen.getByTestId("forge-token"), "AAA");
  await userEvent.click(screen.getByTestId("forge-save"));

  // …and the operator keeps typing while it is in flight.
  await userEvent.type(screen.getByTestId("forge-token"), "BBB");
  release?.({});

  await waitFor(() => expect(screen.getByTestId("forge-saved")).toBeInTheDocument());
  // B survives, and the editor stays open on it.
  expect(screen.getByTestId("forge-token")).toHaveValue("AAABBB");
});

test("a token that is UNCHANGED when the save lands is cleared, as before", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({} as never);
  render(<ForgeSettings />);
  await userEvent.click(screen.getByTestId("forge-token-edit"));
  await userEvent.type(screen.getByTestId("forge-token"), "AAA");
  await userEvent.click(screen.getByTestId("forge-save"));
  await waitFor(() => expect(screen.queryByTestId("forge-token")).toBeNull());
});
