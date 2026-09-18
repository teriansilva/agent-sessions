import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import { ArchiveMissionsCard, CompactDatabase, PruneCard } from "./MaintenanceCards";

/** Settings → Maintenance, the Prune card's one safety rule (#993, reviews 4903/4915/4919):
 *  **an unmeasured category is never submitted as zero.** `{items: 0}` is a count; a failed,
 *  missing or never-obtained measurement is not, and the confirmation totals only what it could
 *  measure — so a prune authorised by that total would delete contents nobody counted. */

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      compactInfo: vi.fn(),
      compact: vi.fn(),
      pruneInfo: vi.fn(),
      prune: vi.fn(),
      archiveOldMissionsInfo: vi.fn(),
      archiveOldMissions: vi.fn(),
    },
  };
});

type Info = Awaited<ReturnType<typeof api.pruneInfo>>;

function info(over: Partial<Info["categories"]> = {}): Info {
  return {
    categories: {
      stale_sockets: { items: 3, bytes: 0 },
      archived_scrollback: { items: 2, bytes: 4096 },
      ...over,
    },
    runner: null,
  };
}

const ERRORED = { items: 0, bytes: 0, error: "the runtime dir could not be read" };

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  vi.mocked(api.compactInfo).mockRejectedValue(new Error("no database"));
  vi.mocked(api.prune).mockResolvedValue({
    removed: 0,
    bytes_freed: 0,
    skipped: [],
    failed: [],
    failed_total: 0,
  });
});

test("a category whose measurement failed blocks the prune it would have understated", async () => {
  // `stale_sockets` is the card's default selection, so an error on it is the live case.
  vi.mocked(api.pruneInfo).mockResolvedValue(info({ stale_sockets: ERRORED }));
  render(<PruneCard />);

  expect(await screen.findByTestId("prune-count-stale_sockets")).toHaveTextContent(
    "couldn’t measure",
  );
  expect(screen.getByText(/contents are unknown/)).toBeInTheDocument();

  const button = screen.getByRole("button", { name: /Prune selected/ });
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(api.prune).not.toHaveBeenCalled();
});

test("a rejected dry run is not an empty cache — nothing is submitted", async () => {
  // HONEST NOTE: this one passes against the pre-fix component too — with `info` null the button
  // is already disabled by `!info`. It is kept for the failed-dry-run copy and the "nothing is
  // submitted" invariant, not as a regression. The two tests that DO pin finding 5 are the
  // missing-category and deferred-rejection cases below.
  vi.mocked(api.pruneInfo).mockRejectedValue(new Error("dry run rejected"));
  render(<PruneCard />);

  expect(await screen.findByText(/Couldn’t measure the caches/)).toBeInTheDocument();

  const button = screen.getByRole("button", { name: /Prune selected/ });
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(api.prune).not.toHaveBeenCalled();
});

test("a category the payload never measured is not submitted as zero", async () => {
  // A payload that OMITS a selected category leaves the per-category error filter empty —
  // `info.categories[id]` is `undefined`, not an error — so the pre-fix guard concluded "nothing
  // failed". The OTHER category's non-zero total then kept `items !== 0`, the confirmation opened
  // reading "Permanently remove 2 items", and the POST went out covering a category nobody had
  // counted. `{items: 0}` is a count; a missing entry is not.
  //
  // The cast is the point: this is a payload that violates the declared shape, which is precisely
  // what a runtime guard exists to survive. Note the button's own `disabled` expression is
  // unchanged by the fix — what changes is that `confirmOpen` never becomes true — so the
  // assertion is on the confirmation, not on the button.
  vi.mocked(api.pruneInfo).mockResolvedValue({
    categories: { archived_scrollback: { items: 2, bytes: 4096 } },
    runner: null,
  } as unknown as Info);
  render(<PruneCard />);

  // Select the measured category alongside the default (unmeasured) `stale_sockets`, so the
  // running total is non-zero and `items === 0` cannot be what blocks the prune.
  await userEvent.click(await screen.findByRole("checkbox", { name: /Archived sessions/ }));
  await userEvent.click(screen.getByRole("button", { name: /Prune selected/ }));

  expect(screen.queryByText(/Permanently remove/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Confirm prune/ })).not.toBeInTheDocument();
  expect(api.prune).not.toHaveBeenCalled();
});

test("a held refresh that rejects while the confirmation is open invalidates it", async () => {
  // Review 4915 finding 5's own repro. The enabling move is NOT a refetch during the confirmation
  // — it is a SELECTION change, which recomputes every guard without issuing a GET:
  //
  //   1. a payload where `stale_sockets` errored renders Refresh and disables the action;
  //   2. Refresh issues a GET that is then held — and nothing supersedes it, so the effect's
  //      `cancelled` guard never fires (it only drops a request a NEWER one replaced), leaving
  //      this rejection live;
  //   3. deselecting the errored category and selecting the measured one empties `unmeasured` and
  //      makes `items` non-zero, so the action enables and the confirmation opens; then
  //   4. the held GET rejects, nulling `info`.
  //
  // Pre-fix, `unmeasured` was computed through `info?.` and so was empty over a null `info`: the
  // confirmation stayed open reading "Permanently remove 0 items (0 B)" and `run()` submitted
  // `api.prune(["archived_scrollback"])` backed by no successful measurement at all.
  let rejectHeld: (e: Error) => void = () => {};
  const held = new Promise<Info>((_resolve, reject) => {
    rejectHeld = reject;
  });
  held.catch(() => {}); // the component owns this rejection

  vi.mocked(api.pruneInfo)
    .mockResolvedValueOnce(info({ stale_sockets: ERRORED }))
    .mockReturnValueOnce(held);

  render(<PruneCard />);

  await userEvent.click(
    await screen.findByRole("button", { name: /Refresh the cache measurements/ }),
  );

  // A checkbox issues no request, so the held GET above is still the latest one in flight.
  await userEvent.click(screen.getByRole("checkbox", { name: /Stale terminal sockets/ }));
  await userEvent.click(screen.getByRole("checkbox", { name: /Archived sessions/ }));
  await userEvent.click(screen.getByRole("button", { name: /Prune selected/ }));
  expect(screen.getByText(/Permanently remove/)).toBeInTheDocument();

  await act(async () => {
    rejectHeld(new Error("refresh rejected"));
  });

  expect(screen.queryByText(/Permanently remove/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Confirm prune/ })).not.toBeInTheDocument();
  expect(api.prune).not.toHaveBeenCalled();
});

test("a fully measured card still prunes — the guard is not a blanket refusal", async () => {
  vi.mocked(api.pruneInfo).mockResolvedValue(info());
  render(<PruneCard />);

  await userEvent.click(await screen.findByRole("button", { name: /Prune selected/ }));
  await userEvent.click(screen.getByRole("button", { name: /Confirm prune/ }));

  expect(api.prune).toHaveBeenCalledWith(["stale_sockets"]);
});

/** A dry run that answers 200 with a body missing the fields the card totals (a version-skewed
 *  server, a proxy's stub, a mock that fulfils every unknown `/api/**` with `{}`) used to throw
 *  inside render — `info.categories[…]` and `info.unresolved.length` on an object that has
 *  neither. The throw took the whole Settings route down to its error boundary, which is how CI
 *  found it: #1007's `overview-warm-invalidation` spec clicks "Archive older" in the SAME panel,
 *  and the boundary detached that button mid-click. An unusable dry run is the "couldn't measure"
 *  case the card already has a path for — absence of evidence is still not evidence of an empty
 *  cache (#1000 reviews 4903/4915/4919). */
test("a 200 dry run missing its counts is unmeasured, not zero — and never crashes the panel", async () => {
  vi.mocked(api.pruneInfo).mockResolvedValue({} as unknown as Info);
  render(<PruneCard />);

  expect(await screen.findByText(/Couldn’t measure the caches/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Prune selected/ })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: /Prune selected/ }));
  expect(api.prune).not.toHaveBeenCalled();
});

test("the missions card treats a 200 dry run missing its counts the same way", async () => {
  vi.mocked(api.archiveOldMissionsInfo).mockResolvedValue(
    {} as unknown as Awaited<ReturnType<typeof api.archiveOldMissionsInfo>>,
  );
  render(<ArchiveMissionsCard />);

  expect(await screen.findByText(/Couldn’t count missions/)).toBeInTheDocument();
  // The action stays visible but blocked: no dry run means no number to confirm against.
  const button = screen.getByRole("button", { name: /Archive old missions/ });
  expect(button).toBeDisabled();
  await userEvent.click(button);
  expect(screen.queryByRole("button", { name: /Confirm mission archive/ })).toBeNull();
  expect(api.archiveOldMissions).not.toHaveBeenCalled();
});

function compactInfo(): Awaited<ReturnType<typeof api.compactInfo>> {
  return {
    compact: {
      available: true,
      db_bytes: 8 * 1024 ** 3,
      wal_bytes: 0,
      reclaimable_bytes: 2 * 1024 ** 3,
      holders: { pids: [], unknown: false },
      blockers: [],
      disk: {
        shared_filesystem: true,
        database_required: 24 * 1024 ** 3,
        database_free: 100 * 1024 ** 3,
        temp_required: 0,
        temp_free: 100 * 1024 ** 3,
      },
    },
    job: null,
    runner: null,
  };
}

function compactJob(state: "vacuum" | "done" = "vacuum") {
  return {
    id: "job-one",
    state,
    started_at: 1,
    finished_at: state === "done" ? 2 : null,
    result:
      state === "done"
        ? {
            vacuum: "done" as const,
            checkpoint: "deferred" as const,
            checkpoint_result: [1, 20, 10],
            bytes_freed: 1024,
            blockers: [],
          }
        : null,
  };
}

test("compaction shows all blockers and refuses unknown measurements", async () => {
  const r = compactInfo();
  r.compact.available = false;
  r.compact.holders = { pids: [14], unknown: true };
  r.compact.disk!.database_free = 0;
  r.compact.disk!.temp_free = 0;
  r.compact.blockers = [
    { code: "held", detail: "A process holds the database." },
    { code: "unknown", detail: "Other processes could not be inspected." },
    { code: "space", detail: "Not enough disk space." },
  ];
  vi.mocked(api.compactInfo).mockResolvedValue(r);
  render(<CompactDatabase />);
  for (const b of r.compact.blockers)
    expect(await screen.findByText(b.detail)).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Compact database" }),
  ).toBeDisabled();
  expect(api.compact).not.toHaveBeenCalled();
});

test("compaction requires confirmation and polls its own job to the separate checkpoint outcome", async () => {
  vi.mocked(api.compactInfo).mockResolvedValue(compactInfo());
  vi.mocked(api.compact).mockResolvedValue({
    job: compactJob(),
    runner: { job: "opencode_compact", started_at: 1 },
  });
  render(<CompactDatabase />);
  await userEvent.click(
    await screen.findByRole("button", { name: "Compact database" }),
  );
  expect(api.compact).not.toHaveBeenCalled();
  vi.mocked(api.compactInfo).mockResolvedValue({
    ...compactInfo(),
    job: compactJob("done"),
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Confirm compaction" }),
  );
  expect(api.compact).toHaveBeenCalledTimes(1);
  expect(await screen.findByText(/Compaction completed/)).toBeVisible();
  expect(screen.getByText(/WAL checkpoint deferred/)).toBeVisible();
  expect(api.compactInfo).toHaveBeenLastCalledWith("job-one");
});

test("a lost POST response requires refresh without resubmission", async () => {
  vi.mocked(api.compactInfo).mockResolvedValue(compactInfo());
  vi.mocked(api.compact).mockRejectedValue(new Error("connection lost"));
  render(<CompactDatabase />);
  await userEvent.click(
    await screen.findByRole("button", { name: "Compact database" }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Confirm compaction" }),
  );
  expect(
    await screen.findByText(/Couldn’t confirm whether compaction started/),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Compact database" }),
  ).toBeDisabled();
  vi.mocked(api.compactInfo).mockResolvedValue({
    ...compactInfo(),
    job: compactJob("done"),
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Refresh the database status" }),
  );
  expect(await screen.findByText(/Compaction completed/)).toBeVisible();
  expect(api.compact).toHaveBeenCalledTimes(1);
});

test.each(["before", "during"])(
  "a status request started %s submission cannot clear a later uncertain outcome",
  async (when) => {
    type Status = Awaited<ReturnType<typeof api.compactInfo>>;
    let resolveStatus!: (value: Status) => void;
    const heldStatus = new Promise<Status>((resolve) => {
      resolveStatus = resolve;
    });
    let rejectPost!: (error: Error) => void;
    const heldPost = new Promise<Awaited<ReturnType<typeof api.compact>>>((_, reject) => {
      rejectPost = reject;
    });
    vi.mocked(api.compactInfo).mockResolvedValue(compactInfo());
    vi.mocked(api.compact).mockReturnValueOnce(heldPost);
    render(<CompactDatabase />);
    const start = () => screen.getByRole("button", { name: "Compact database" });
    await waitFor(() => expect(start()).toBeEnabled());
    const refresh = () =>
      userEvent.click(
        screen.getByRole("button", { name: "Refresh the database status" }),
      );
    vi.mocked(api.compactInfo).mockReturnValueOnce(heldStatus);
    if (when === "before") await refresh();
    await userEvent.click(start());
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm compaction" }),
    );
    if (when === "during") await refresh();
    expect(api.compactInfo).toHaveBeenCalledTimes(2);
    await act(async () => {
      rejectPost(new Error("response lost"));
    });
    const uncertainty = /Couldn’t confirm whether compaction started/;
    expect(screen.getByText(uncertainty)).toBeVisible();
    expect(start()).toBeDisabled();

    // This snapshot predates the uncertain outcome even though it arrives afterward.
    await act(async () => {
      resolveStatus(compactInfo());
    });
    expect(screen.getByText(uncertainty)).toBeVisible();
    expect(start()).toBeDisabled();
    expect(api.compact).toHaveBeenCalledTimes(1);

    vi.mocked(api.compactInfo).mockResolvedValueOnce({
      ...compactInfo(),
      job: compactJob("done"),
    });
    await refresh();
    expect(await screen.findByText(/Compaction completed/)).toBeVisible();
    expect(screen.queryByText(uncertainty)).not.toBeInTheDocument();
    expect(start()).toBeEnabled();
    expect(api.compact).toHaveBeenCalledTimes(1);
  },
);

test("a replaced job remains explicitly unavailable across remount, never another job's result", async () => {
  sessionStorage.setItem("tr-maintenance-compact-job", "job-old");
  vi.mocked(api.compactInfo).mockRejectedValue(
    new ApiError(404, "unavailable"),
  );
  render(<CompactDatabase />);
  expect(
    await screen.findByText(
      /previous compaction result is no longer available/,
    ),
  ).toBeVisible();
  expect(api.compactInfo).toHaveBeenCalledWith("job-old");
  expect(
    screen.getByRole("button", { name: "Compact database" }),
  ).toBeDisabled();
  vi.mocked(api.compactInfo).mockResolvedValue({
    ...compactInfo(),
    job: compactJob("done"),
  });
  await userEvent.click(
    screen.getByRole("button", { name: "Refresh the database status" }),
  );
  expect(await screen.findByText(/Compaction completed/)).toBeVisible();
  expect(
    screen.getByText(/previous compaction result is no longer available/),
  ).toBeVisible();
});

test("malformed compaction data never crashes Settings or enables a mutation", async () => {
  vi.mocked(api.compactInfo).mockResolvedValue(
    {} as Awaited<ReturnType<typeof api.compactInfo>>,
  );
  render(<CompactDatabase />);
  expect(
    await screen.findByText(/Couldn’t refresh the database status/),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Compact database" }),
  ).toBeDisabled();
});
