import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import type { GitStatus } from "../../types/api";
import { GitTab } from "./GitTab";

vi.mock("../../lib/api", () => ({
  ApiError: class ApiError extends Error {},
  api: {
    gitPull: vi.fn(),
    gitPushTarget: vi.fn(() => new Promise(() => {})), // never settles: the preflight is not under test
    gitBranches: vi.fn(() => new Promise(() => {})),
  },
}));
const { api } = await import("../../lib/api");

function status(over: Partial<GitStatus> = {}): GitStatus {
  return {
    repo: "/home/u/proj",
    branch: "main",
    upstream: "origin/main",
    ahead: 0,
    behind: 1,
    entries: [],
    truncated: false,
    ...over,
  };
}

function props(root: string) {
  return {
    root,
    sessionKey: "claude:abc",
    status: status({ repo: root }),
    loading: false,
    error: null,
    onOpen: () => {},
    onRetry: () => {},
  };
}

test("a write that finishes after the panel moved away does not lock the repo it started in", async () => {
  // #806 review: `busy` is derived as `op.root === root`, so an operation left sitting at root A
  // is not gone when the panel navigates away — it is DORMANT. Coming back to A made it current
  // again with `busy` still set, and every write control stayed disabled for the rest of the
  // session. A → B → A is the whole bug; a completion that merely arrives late is not.
  let finish: (v: unknown) => void = () => {};
  vi.mocked(api.gitPull).mockReturnValue(
    new Promise((res) => {
      finish = res;
    }) as ReturnType<typeof api.gitPull>,
  );

  const view = render(<GitTab {...props("/home/u/A")} />);
  const pull = () => screen.getByRole("button", { name: /PULL/i });
  await userEvent.click(pull());
  expect(pull()).toBeDisabled(); // the operation is genuinely in flight

  view.rerender(<GitTab {...props("/home/u/B")} />); // navigate away, still in flight
  finish({ status: status({ repo: "/home/u/A" }), upstream: "origin/main" });
  await waitFor(() => expect(vi.mocked(api.gitPull)).toHaveBeenCalled());

  view.rerender(<GitTab {...props("/home/u/A")} />); // and back
  await waitFor(() => expect(pull()).not.toBeDisabled());
});

test("a null post-write status is never handed up as the panel's new truth", async () => {
  // The server answers `status: null` when the operation landed but the post-write read failed.
  // Passing that up replaced the panel's real status with an absence — a successful pull blanking
  // the repository it had just pulled. The write happened; there is simply nothing newer to show.
  const seen: (GitStatus | null)[] = [];
  vi.mocked(api.gitPull).mockResolvedValue({
    status: null,
    upstream: "origin/main",
  } as unknown as Awaited<ReturnType<typeof api.gitPull>>);

  render(
    <GitTab {...props("/home/u/A")} onStatus={(s) => seen.push(s)} />,
  );
  await userEvent.click(screen.getByRole("button", { name: /PULL/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /PULL/i })).not.toBeDisabled());
  expect(seen).toEqual([]);
});

// NOT PRESENT: a regression for the commit -> passive-effect gap that motivated the
// `useLayoutEffect` fence in GitTab. One was written and then removed, because it passed against
// the effect-based version and so proved nothing. Measured, in this environment: `flushSync`
// flushes pending passive effects as part of its own work (a probe read the ref as "B"
// immediately after `flushSync`, even with a passive effect), and a `startTransition` render does
// not commit at all under jsdom without `act` — so neither route opens the window. The fix is
// kept because it is strictly safer and free; it is recorded here as UNPROVEN rather than dressed
// up as covered.
