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
    gitCommit: vi.fn(),
    gitCommitPaths: vi.fn(),
    gitSettle: vi.fn(),
    gitDiscard: vi.fn(),
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

// ---------------------------------------------------------------- commit selected / settle / revert (#950)

const HEAD = "c".repeat(40);
const e950 = (over: Partial<GitStatus["entries"][number]>) => ({
  path: "a.txt",
  index: ".",
  worktree: "M",
  kind: "changed" as const,
  oid: "o",
  fp: "fp-a",
  ...over,
});
const dirty950 = (over: Partial<GitStatus> = {}) =>
  status({
    head: HEAD,
    staged_fp: "staged-fp",
    dirty_fp: "dirty-fp",
    unsettled: [],
    entries: [
      e950({}),
      e950({ path: "b.txt", index: "M", worktree: ".", kind: "staged", fp: "fp-b" }),
      e950({ path: "c.txt", index: "?", worktree: "?", kind: "untracked", oid: null, fp: "fp-c" }),
    ],
    ...over,
  });
const ok = (over: Record<string, unknown> = {}) =>
  ({ status: dirty950({ entries: [] }), commit: "abc1234", files: 2, index: "settled", ...over }) as never;

test("COMMIT SELECTED sends the ticked paths, their fingerprints and the head the panel showed", async () => {
  vi.mocked(api.gitCommitPaths).mockResolvedValue(ok());
  render(<GitTab {...props("/home/u/proj")} status={dirty950()} />);
  await userEvent.click(screen.getByLabelText("Select a.txt"));
  await userEvent.click(screen.getByLabelText("Select c.txt"));
  await userEvent.click(screen.getByRole("radio", { name: /Selected/ }));
  await userEvent.type(screen.getByLabelText("Commit message"), "tuned");
  await userEvent.click(screen.getByRole("button", { name: /COMMIT 2 SELECTED/ }));
  await waitFor(() =>
    expect(vi.mocked(api.gitCommitPaths)).toHaveBeenCalledWith(
      "/home/u/proj",
      "tuned",
      ["a.txt", "c.txt"],
      { "a.txt": "fp-a", "c.txt": "fp-c" },
      HEAD,
    ),
  );
});

test("ALL commits staged and changed paths but leaves an unticked untracked file out", async () => {
  vi.mocked(api.gitCommitPaths).mockResolvedValue(ok());
  render(<GitTab {...props("/home/u/proj")} status={dirty950()} />);
  await userEvent.click(screen.getByRole("radio", { name: /All/ }));
  await userEvent.type(screen.getByLabelText("Commit message"), "all of it");
  await userEvent.click(screen.getByRole("button", { name: /COMMIT ALL 2/ }));
  await waitFor(() =>
    expect(vi.mocked(api.gitCommitPaths)).toHaveBeenLastCalledWith(
      "/home/u/proj",
      "all of it",
      ["a.txt", "b.txt"],
      { "a.txt": "fp-a", "b.txt": "fp-b" },
      HEAD,
    ),
  );
});

test("INDEX PENDING says both readings and SETTLE settles the commit the panel showed", async () => {
  vi.mocked(api.gitSettle).mockResolvedValue(ok({ index: "settled" }));
  render(<GitTab {...props("/home/u/proj")} status={dirty950({ unsettled: ["b.txt"] })} />);
  const banner = document.querySelector("[data-git-pending]") as HTMLElement;
  expect(banner).not.toBeNull();
  expect(banner.textContent).toMatch(/on purpose/);
  await userEvent.click(screen.getByRole("button", { name: /SETTLE/ }));
  await waitFor(() => expect(vi.mocked(api.gitSettle)).toHaveBeenCalledWith("/home/u/proj", HEAD));
});

test("a STAGED commit with unsettled paths asks first, and names them, before committing", async () => {
  vi.mocked(api.gitCommit).mockReset();
  vi.mocked(api.gitCommit).mockResolvedValue(ok());
  render(<GitTab {...props("/home/u/proj")} status={dirty950({ unsettled: ["b.txt"] })} />);
  await userEvent.type(screen.getByLabelText("Commit message"), "on purpose");
  await userEvent.click(screen.getByRole("button", { name: /^COMMIT$/ }));
  const ask = document.querySelector("[data-unsettled-confirm]") as HTMLElement;
  expect(ask).not.toBeNull();
  expect(ask.textContent).toContain("b.txt");
  expect(vi.mocked(api.gitCommit)).not.toHaveBeenCalled();
  await userEvent.click(document.querySelector("[data-unsettled-go]") as HTMLElement);
  await waitFor(() =>
    expect(vi.mocked(api.gitCommit)).toHaveBeenCalledWith("/home/u/proj", "on purpose", "staged-fp"),
  );
});

test("REVERT on a staged row confirms and sends from=head with the commit it named", async () => {
  vi.mocked(api.gitDiscard).mockResolvedValue(ok({ index: "settled", recoverable: { "b.txt": ["1234567890ab"] } }));
  render(<GitTab {...props("/home/u/proj")} status={dirty950()} />);
  await userEvent.click(screen.getByRole("button", { name: "Revert b.txt to the last commit" }));
  expect(document.querySelector("[data-revert-confirm]")).not.toBeNull();
  await userEvent.click(document.querySelector("[data-revert-go]") as HTMLElement);
  await waitFor(() =>
    expect(vi.mocked(api.gitDiscard)).toHaveBeenCalledWith(
      "/home/u/proj",
      ["b.txt"],
      { "b.txt": "fp-b" },
      "head",
      HEAD,
    ),
  );
});
