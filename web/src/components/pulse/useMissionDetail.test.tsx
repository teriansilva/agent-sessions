/** The mission loader's three cadences (#889).
 *
 *  The full load runs ONCE per mission, which is right for data the operator changes. Two things
 *  change underneath a console nobody is touching, and each gets its own cadence:
 *
 *   - the supervisor's reading, on the slow detail poll (#885);
 *   - the OBJECTIVE list while its producer is still running, which is what this file is about.
 *
 *  `POST /api/missions` returns before the objectives exist, so a new mission legitimately shows
 *  "working out what done means…". On the detail cadence alone that sentence stayed on screen for
 *  minutes after the list had arrived — the pane was accurate exactly once, at mount.
 *
 *  …and, from #890/#902, the OPEN-TURN cadence: a turn in flight is polled on the same short
 *  interval and stops the moment it settles. All three cadences write the mission row, which is
 *  why they all go through `installMission` and its ticket.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";

import { useMissionDetail } from "./useMissionDetail";

/** The bound the hook enforces. Named here so the drive below cannot silently stop short of it. */
const TURN_POLL_MAX = 60;

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      mission: vi.fn(),
      missionObjectives: vi.fn(),
      missionContext: vi.fn(),
    },
  };
});

const CTX = {
  id: "msn_1",
  project_id: "",
  cwd: "/repo",
  sessions: [],
  git: null,
  git_error: null,
};

const RUNNING_TURN = {
  turn_id: "t1",
  state: "in_progress",
  text: "run the tests",
  delivery_error: "",
  created_at: 1,
};

function detail(objectivesState: string | null, turn: unknown = null) {
  return {
    id: "msn_1",
    state: "running",
    objectives_state: objectivesState,
    sessions: [],
    events: [],
    events_next_seq: null,
    turn,
  };
}

/** A mission row that is only about its TURN. The objective state is settled, so the pending
 *  poll stays out of the way of a test about the turn cadence. */
function turnDetail(turn: unknown) {
  return detail("done", turn);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.missionContext).mockResolvedValue(CTX as never);
  vi.mocked(api.missionObjectives).mockResolvedValue({ objectives: [] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

test("a PENDING objective list is re-read on its own fast cadence", async () => {
  vi.mocked(api.mission).mockResolvedValue(detail("pending") as never);
  renderHook(() => useMissionDetail("msn_1"));

  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalledTimes(1));
  const afterMount = vi.mocked(api.missionObjectives).mock.calls.length;

  // Well inside the 150s detail cadence — on that alone, nothing would have been re-read.
  await vi.advanceTimersByTimeAsync(10_000);
  await waitFor(() =>
    expect(vi.mocked(api.missionObjectives).mock.calls.length).toBeGreaterThan(
      afterMount,
    ),
  );
  // The mission row is re-read too: it carries `objectives_state`, so it is the only thing that
  // can END the wait. Polling the list alone would leave the pane saying "working out what done
  // means" over a list that had already arrived.
  expect(vi.mocked(api.mission).mock.calls.length).toBeGreaterThan(1);
});

test("it STOPS the moment the producer settles", async () => {
  vi.mocked(api.mission).mockResolvedValue(detail("pending") as never);
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalled());

  await vi.advanceTimersByTimeAsync(10_000);
  await waitFor(() =>
    expect(vi.mocked(api.missionObjectives).mock.calls.length).toBeGreaterThan(
      1,
    ),
  );

  // The producer finishes.
  vi.mocked(api.mission).mockResolvedValue(detail("done") as never);
  await vi.advanceTimersByTimeAsync(5_000);
  await waitFor(() =>
    expect(vi.mocked(api.mission).mock.results.at(-1)).toBeTruthy(),
  );
  const settled = vi.mocked(api.missionObjectives).mock.calls.length;

  await vi.advanceTimersByTimeAsync(30_000);
  // No further polling. A cadence that kept running after the wait ended would hit two endpoints
  // every three seconds for the life of the page.
  expect(vi.mocked(api.missionObjectives).mock.calls.length).toBe(settled);
});

test("a list that never settles is BOUNDED, not polled for ever", async () => {
  vi.mocked(api.mission).mockResolvedValue(detail("pending") as never);
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalled());

  // A `pending` that never settles is a real state: the producer can die, and `recover_pending`
  // re-drives it only at startup. Ten minutes of a 3s cadence would be 200 rounds.
  await vi.advanceTimersByTimeAsync(600_000);
  const calls = vi.mocked(api.missionObjectives).mock.calls.length;
  // 1 mount + at most OBJECTIVES_POLL_MAX (40) attempts.
  expect(calls).toBeLessThanOrEqual(41);
});

test("a settled mission does no fast polling at all", async () => {
  vi.mocked(api.mission).mockResolvedValue(detail("done") as never);
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalledTimes(1));

  await vi.advanceTimersByTimeAsync(60_000);
  // The mount load and nothing else. The ordinary detail cadence is 150s and is not this.
  expect(vi.mocked(api.missionObjectives).mock.calls.length).toBe(1);
});

// ---- finding 2 of #896's review: a pre-edit poll must not resurrect the old list ------------

test("a poll in flight when `reload` runs cannot restore the objectives it fetched", async () => {
  // The exact ordering the review reproduced: the operator edits an objective, the reload fetches
  // the NEW list, and a poll issued BEFORE the edit resolves afterwards carrying the OLD one.
  vi.mocked(api.mission).mockResolvedValue(detail("pending") as never);

  let releaseStale: ((v: { objectives: unknown[] }) => void) | undefined;
  const stale = new Promise<{ objectives: unknown[] }>(
    (r) => (releaseStale = r),
  );
  vi.mocked(api.missionObjectives)
    .mockResolvedValueOnce({ objectives: [{ key: "old" }] } as never) // mount
    .mockReturnValueOnce(stale as never) // the poll we hold open
    .mockResolvedValue({ objectives: [{ key: "new" }] } as never); // everything after the reload

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.objectives).toHaveLength(1));

  // Let the poll fire and BLOCK.
  await vi.advanceTimersByTimeAsync(3_500);
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalledTimes(2));

  // The operator's edit lands and the console re-reads.
  await act(async () => {
    result.current.reload();
  });
  await waitFor(() =>
    expect(result.current.objectives).toEqual([{ key: "new" }]),
  );

  // …and only NOW does the pre-edit poll answer.
  await act(async () => {
    releaseStale?.({ objectives: [{ key: "old" }] });
    await Promise.resolve();
  });
  await vi.advanceTimersByTimeAsync(50);

  // It must change nothing. `reload` bumps the epoch both effects key on, so the stale response
  // resolves into a torn-down effect whose `live` flag is already false.
  expect(result.current.objectives).toEqual([{ key: "new" }]);
});

test("polling stops only on a list read that STARTED after the settlement", async () => {
  // #896 review 3, finding 1: fired in parallel, the objectives request can snapshot BEFORE the
  // producer's transaction while the mission request snapshots after it — so the hook sees `done`,
  // stops, and keeps an empty list for ever. Settled, and wrong.
  const order: string[] = [];
  vi.mocked(api.mission).mockImplementation(async () => {
    order.push("mission");
    return detail(
      order.filter((o) => o === "mission").length > 1 ? "done" : "pending",
    ) as never;
  });
  vi.mocked(api.missionObjectives).mockImplementation(async () => {
    order.push("objectives");
    // Only a read that starts AFTER the mission reported `done` can see the row.
    const settled =
      order.lastIndexOf("mission") >= 0 &&
      order.filter((o) => o === "mission").length > 1;
    return { objectives: settled ? [{ key: "arrived" }] : [] } as never;
  });

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalled());
  await vi.advanceTimersByTimeAsync(4_000);
  await waitFor(() =>
    expect(result.current.objectives).toEqual([{ key: "arrived" }]),
  );

  // Each tick reads the mission and THEN the objectives — never the other way round, and never
  // both at once.
  const mi = order.indexOf("mission");
  expect(order[mi + 1]).toBe("objectives");
});

test("a DELAYED settled-objectives response is not discarded by its own settlement", async () => {
  // #896 review 4, finding 1. The previous test resolves the objectives mock immediately, so the
  // settled list lands in the same microtask as the mission that settles it and the ordering
  // never shows. DEFER it: with the row installed as soon as it is read, `pending` flips false,
  // the poll effect is torn down, and the effect's own `live` flag then rejects the response it
  // was waiting for — the hook finishes at `done` with the empty list it started with.
  let missionReads = 0;
  vi.mocked(api.mission).mockImplementation(async () => {
    missionReads += 1;
    return detail(missionReads > 1 ? "done" : "pending") as never;
  });

  let releaseSettled: (() => void) | null = null;
  vi.mocked(api.missionObjectives).mockImplementation(async () => {
    if (missionReads < 2) return { objectives: [] } as never;
    // The read that happens BECAUSE of the settlement, held open across the render that would
    // tear the poll down.
    await new Promise<void>((resolve) => {
      releaseSettled = resolve;
    });
    return { objectives: [{ key: "arrived" }] } as never;
  });

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.missionObjectives).toHaveBeenCalled());
  await vi.advanceTimersByTimeAsync(4_000);
  await waitFor(() => expect(releaseSettled).not.toBeNull());

  // The wait is STILL ON SCREEN while the paired list is in flight — the settlement is not
  // installed ahead of the list it refers to, so there is no render in which the poll is torn
  // down and the response still outstanding.
  expect(result.current.mission?.objectives_state).toBe("pending");

  await act(async () => {
    releaseSettled?.();
    await Promise.resolve();
  });

  await waitFor(() =>
    expect(result.current.objectives).toEqual([{ key: "arrived" }]),
  );
  expect(result.current.mission?.objectives_state).toBe("done");
});

test("the SLOW detail poll cannot settle the producer on a stale list either", async () => {
  // The same rule through the other door, and it is reachable: the fast poll is BOUNDED, so a
  // producer that takes longer than its budget leaves the 150s detail poll as the only reader.
  // Installing a `done` row from there settles the pane over whatever list the hook happens to
  // be holding — which, for a producer that had not finished, is the empty one.
  let settled = false;
  vi.mocked(api.mission).mockImplementation(
    async () => detail(settled ? "done" : "pending") as never,
  );
  vi.mocked(api.missionObjectives).mockImplementation(async () =>
    settled
      ? ({ objectives: [{ key: "arrived" }] } as never)
      : ({ objectives: [] } as never),
  );

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.mission).not.toBeNull());

  // Run the fast poll past its budget: 40 attempts at 3s, and it stops asking.
  await vi.advanceTimersByTimeAsync(3_000 * 45);
  const spent = vi.mocked(api.missionObjectives).mock.calls.length;
  await vi.advanceTimersByTimeAsync(3_000 * 5);
  expect(vi.mocked(api.missionObjectives).mock.calls.length).toBe(spent);
  expect(result.current.mission?.objectives_state).toBe("pending");

  // NOW the producer finishes. Only the slow poll is left to notice.
  settled = true;
  await vi.advanceTimersByTimeAsync(160_000);
  await waitFor(() =>
    expect(result.current.mission?.objectives_state).toBe("done"),
  );
  expect(result.current.objectives).toEqual([{ key: "arrived" }]);
});

test("a HELD full-load objectives response cannot overwrite the settled list", async () => {
  // #896 review 5, finding 1. The mount's own objectives request is a third writer, and it had
  // only the effect-level `live` flag: it can snapshot BEFORE the producer settles and resolve
  // AFTER the poll has installed `done` plus the new list, restoring the empty one with no poll
  // left to repair it. `live` orders an effect's lifetime; ordering responses issued by
  // different code paths needs a generation.
  let missionReads = 0;
  vi.mocked(api.mission).mockImplementation(async () => {
    missionReads += 1;
    return detail(missionReads > 1 ? "done" : "pending") as never;
  });

  let releaseFirst: (() => void) | null = null;
  let calls = 0;
  vi.mocked(api.missionObjectives).mockImplementation(async () => {
    calls += 1;
    if (calls === 1) {
      // THE MOUNT LOAD, held open across the settlement.
      await new Promise<void>((resolve) => {
        releaseFirst = resolve;
      });
      return { objectives: [{ key: "stale" }] } as never;
    }
    return {
      objectives: missionReads > 1 ? [{ key: "arrived" }] : [],
    } as never;
  });

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(releaseFirst).not.toBeNull());
  await vi.advanceTimersByTimeAsync(4_000);
  await waitFor(() =>
    expect(result.current.objectives).toEqual([{ key: "arrived" }]),
  );

  // NOW the mount's request lands, carrying the list as it was before the producer finished.
  await act(async () => {
    releaseFirst?.();
    await Promise.resolve();
  });
  await vi.advanceTimersByTimeAsync(100);
  expect(result.current.objectives).toEqual([{ key: "arrived" }]);
});

test("a page issued before a reload cannot append onto the list that replaced it", async () => {
  // #896 review 5, finding 2. A page is an extension of the list it was computed FROM. Applying
  // it to a list that has since been replaced does not duplicate rows — it opens a HOLE: the
  // cursor jumps past events nothing will ever ask for again.
  const page = (from: number) => ({
    ...detail("done"),
    events: [{ seq: from }, { seq: from - 1 }],
    events_next_seq: from - 10,
  });

  let releasePage: (() => void) | null = null;
  vi.mocked(api.mission).mockImplementation(async (_id, opts) => {
    if ((opts as { before?: number } | undefined)?.before != null) {
      await new Promise<void>((resolve) => {
        releasePage = resolve;
      });
      return page(59) as never; // computed from the OLD list
    }
    return page(100) as never;
  });

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.cursor).toBe(90));

  act(() => result.current.loadOlder());
  await waitFor(() => expect(releasePage).not.toBeNull());

  // The reload installs a fresh first page — and a fresh cursor — while the page is in flight.
  act(() => result.current.reload());
  await waitFor(() => expect(result.current.events.length).toBe(2));

  await act(async () => {
    releasePage?.();
    await Promise.resolve();
  });
  await vi.advanceTimersByTimeAsync(100);
  // The superseded page is dropped: the cursor still points at the events after the ones on
  // screen, rather than having jumped past them.
  expect(result.current.cursor).toBe(90);
  expect(result.current.events).toEqual([{ seq: 100 }, { seq: 99 }]);
});

test("a FAILED settlement read is retried on a later detail tick, not lost", async () => {
  // THE SLOW PATH. The fast poll already refuses to settle without a list and tries again next
  // tick — but it is bounded at 40 attempts (120s), and after that a settlement arrives on the
  // ordinary 150s detail tick, where `installMission` fetches the list once. `settles` is true
  // for exactly ONE row, so a failure there used to be the last chance anyone asked: the row was
  // already `done`, every later tick skipped the list, and the fast poll had stopped. The pane
  // sat on `done` over an empty list for the life of the page (#896 review 7, finding 1).
  let producerDone = false;
  vi.mocked(api.mission).mockImplementation(
    async () => detail(producerDone ? "done" : "pending") as never,
  );
  let failNext = false;
  vi.mocked(api.missionObjectives).mockImplementation(async () => {
    if (failNext) {
      failNext = false;
      throw new Error("the store blinked");
    }
    return {
      objectives: producerDone
        ? [{ key: "pr_open", title: "A PR is open" }]
        : [],
    } as never;
  });

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());

  // Past the bounded fast poll, with the producer still working.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(130_000);
  });
  expect(result.current.mission?.objectives_state).toBe("pending");

  // Now it settles, and the read paired with THAT row fails.
  producerDone = true;
  failNext = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150_000);
  });
  // The state is the truth and is installed either way; the list is what is missing.
  await waitFor(() =>
    expect(result.current.mission?.objectives_state).toBe("done"),
  );
  expect(result.current.objectives).toEqual([]);

  // The next ordinary tick pays the debt.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150_000);
  });
  await waitFor(() =>
    expect(result.current.objectives).toEqual([
      { key: "pr_open", title: "A PR is open" },
    ]),
  );
});

test("a settled list that ARRIVED is not asked for again on every tick", async () => {
  // The mirror, and the reason the debt is a flag rather than "always re-read": a settled list is
  // a list the OPERATOR changes, and re-reading it on the supervisor's cadence would multiply the
  // poll cost to keep current something that nothing is changing.
  let producerDone = false;
  vi.mocked(api.mission).mockImplementation(
    async () => detail(producerDone ? "done" : "pending") as never,
  );
  vi.mocked(api.missionObjectives).mockImplementation(
    async () =>
      ({
        objectives: producerDone
          ? [{ key: "pr_open", title: "A PR is open" }]
          : [],
      }) as never,
  );

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(130_000);
  });

  producerDone = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150_000);
  });
  await waitFor(() =>
    expect(result.current.mission?.objectives_state).toBe("done"),
  );
  const settled = vi.mocked(api.missionObjectives).mock.calls.length;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(450_000);
  });
  expect(vi.mocked(api.missionObjectives).mock.calls.length).toBe(settled);
});

test("a STALE full-load response cannot clear the settled-list debt", async () => {
  // The debt has to be owed FROM A TICKET, not as a bare boolean (#896 review 8, finding 1).
  //
  // The mount's objective read can snapshot BEFORE the producer settles and resolve long after
  // the settlement read has failed. Its own write is correctly rejected by the order contract —
  // and a boolean debt was still cleared by it, so the next tick saw `done`, no debt, and never
  // asked again. The pane stayed settled over the empty list for the life of the page.
  //
  // The settlement is driven on the SLOW path, because the fast poll deliberately refuses to
  // settle without a list and simply tries again — so a failed paired read there is not the case
  // this is about.
  let producerDone = false;
  vi.mocked(api.mission).mockImplementation(
    async () => detail(producerDone ? "done" : "pending") as never,
  );

  let releaseMount: (v: { objectives: unknown[] }) => void = () => {};
  const mount = new Promise<{ objectives: unknown[] }>((res) => {
    releaseMount = res;
  });
  let first = true;
  let failNext = false;
  vi.mocked(api.missionObjectives).mockImplementation((() => {
    if (first) {
      first = false;
      return mount; // held open across the settlement
    }
    if (failNext) {
      failNext = false;
      return Promise.reject(new Error("the store blinked"));
    }
    // WHILE THE PRODUCER IS STILL WORKING, every other read fails too. That is not incidental:
    // it keeps `objApplied` at zero, so when the mount's stale response finally lands it is
    // ACCEPTED by the order contract rather than rejected — which is the only case in which the
    // ticket comparison in `putObjectives` is what stops it clearing the debt.
    if (!producerDone) return Promise.reject(new Error("still warming up"));
    return Promise.resolve({
      objectives: [{ key: "pr_open", title: "A PR is open" }],
    });
  }) as never);

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());

  // Past the bounded fast poll, producer still working.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(130_000);
  });
  expect(result.current.mission?.objectives_state).toBe("pending");

  // It settles on the ordinary detail tick, and the read paired with THAT row fails.
  producerDone = true;
  failNext = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150_000);
  });
  await waitFor(() =>
    expect(result.current.mission?.objectives_state).toBe("done"),
  );
  expect(result.current.objectives).toEqual([]);

  // ...and NOW the mount's pre-settlement read finally lands. It must not count as payment.
  await act(async () => {
    releaseMount({ objectives: [] });
    await Promise.resolve();
  });
  expect(result.current.objectives).toEqual([]);

  // The next ordinary tick still owes the list, and pays it.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(150_000);
  });
  await waitFor(() =>
    expect(result.current.objectives).toEqual([
      { key: "pr_open", title: "A PR is open" },
    ]),
  );
});

test("a SLOW mission response cannot restore state a newer one replaced", async () => {
  // The objective writes were ticketed; the row, the events and the cursor were not — and two
  // pollers write them (#896 review 10, finding 2). A read that snapshots the OLD picture can
  // land after a newer one has installed, restoring stale lifecycle state and a cursor pointing
  // into a page that is no longer on screen.
  //
  // `live` cannot see this: both responses belong to the SAME effect lifetime.
  // BOTH still `pending`, deliberately: a state change tears the polling effect down, and then
  // `live` alone already drops the late response. The case the ticket is for is two responses
  // within ONE effect lifetime — two overlapping ticks of the same interval, or the slow and
  // fast pollers together.
  const OLD = {
    ...detail("pending"),
    state: "running",
    events: [{ seq: 1, kind: "recap", text: "older" }],
    events_next_seq: 0,
  };
  const NEW = {
    ...detail("pending"),
    state: "review",
    events: [{ seq: 9, kind: "recap", text: "newer" }],
    events_next_seq: 8,
  };
  let releaseSlow: ((v: unknown) => void) | null = null;
  let hold = false;
  let done = false;
  vi.mocked(api.mission).mockImplementation((() => {
    if (hold)
      return new Promise((res) => {
        releaseSlow = res;
      });
    return Promise.resolve((done ? NEW : OLD) as never);
  }) as never);
  vi.mocked(api.missionObjectives).mockResolvedValue({
    objectives: [{ key: "pr_open", title: "A PR is open" }],
  } as never);

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.mission?.state).toBe("running"));

  // A read is issued and HELD, carrying the old picture.
  hold = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000);
  });
  expect(releaseSlow).not.toBeNull();

  // …a later read lands the newer one.
  hold = false;
  done = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000);
  });
  await waitFor(() => expect(result.current.mission?.state).toBe("review"));
  expect(result.current.cursor).toBe(8);

  // …and only now does the held read resolve, with the older picture.
  await act(async () => {
    releaseSlow?.(OLD);
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(result.current.mission?.state).toBe("review");
  expect(result.current.cursor).toBe(8);
  expect(result.current.events.map((e) => e.seq)).toEqual([9]);
});

test("an OPEN TURN is polled on its own short cadence, and the poll stops when it settles", async () => {
  let turn: unknown = RUNNING_TURN;
  vi.mocked(api.mission).mockImplementation(
    async () => turnDetail(turn) as never,
  );

  const { result } = renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(result.current.mission?.turn).toBeTruthy());
  const afterMount = vi.mocked(api.mission).mock.calls.length;

  // Well inside the 150s detail cadence — on that alone nothing would have been re-read.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBeGreaterThan(afterMount);

  // It settles, and the short cadence stops: a console with nothing in flight is exactly as
  // quiet as it was before.
  turn = null;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  await waitFor(() => expect(result.current.mission?.turn).toBeNull());
  const settled = vi.mocked(api.mission).mock.calls.length;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBe(settled);
});

test("the open-turn poll is BOUNDED, so a turn that never settles cannot poll for ever", async () => {
  // A turn that stays open is a real state — a worker can die mid-turn and recovery re-drives it
  // — and an unbounded 3s cadence would hit the endpoint for the life of the page.
  vi.mocked(api.mission).mockImplementation(
    async () => turnDetail(RUNNING_TURN) as never,
  );
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());

  // ONE INTERVAL AT A TIME, not one 210s jump. `advanceTimersByTimeAsync` interleaves the real
  // microtask queue, so a single large advance drains as many poll iterations as the machine
  // happens to get through — 70 on an idle box, fewer on a loaded runner, where the cap was then
  // never reached and this test failed for a reason that had nothing to do with the code. Each
  // step gets its own `act`, so every attempt completes before the next tick is scheduled.
  for (let i = 0; i < TURN_POLL_MAX + 10; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
  }
  const capped = vi.mocked(api.mission).mock.calls.length;
  // …and the cap really did engage, so the window below is measuring a STOPPED poll rather than
  // one that merely had not started.
  expect(capped).toBeLessThanOrEqual(TURN_POLL_MAX + 2);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_000 * 20);
  });
  // Only the ordinary 150s cadence is left running.
  expect(vi.mocked(api.mission).mock.calls.length - capped).toBeLessThanOrEqual(
    1,
  );
});

test("a console with NO open turn keeps the ordinary cadence and nothing else", async () => {
  // The mirror. The short poll is for a conversation in flight; a console watching a board must
  // not pay for it.
  vi.mocked(api.mission).mockImplementation(
    async () => turnDetail(null) as never,
  );
  renderHook(() => useMissionDetail("msn_1"));
  await waitFor(() => expect(api.mission).toHaveBeenCalled());
  const afterMount = vi.mocked(api.mission).mock.calls.length;

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(vi.mocked(api.mission).mock.calls.length).toBe(afterMount);
});
