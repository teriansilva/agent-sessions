import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { Session, SessionsPage } from "../types/api";
import { useSessionsList } from "./useSessionsList";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      archive: vi.fn(),
      unarchive: vi.fn(),
      favorite: vi.fn(),
      unfavorite: vi.fn(),
      setSessionProject: vi.fn(),
    },
  };
});

const mockSessions = vi.mocked(api.sessions);
const mockArchive = vi.mocked(api.archive);

interface Deferred {
  promise: Promise<SessionsPage>;
  resolve: (v: SessionsPage) => void;
}
function deferred(): Deferred {
  let resolve!: (v: SessionsPage) => void;
  const promise = new Promise<SessionsPage>((r) => (resolve = r));
  return { promise, resolve };
}

function sess(title: string): Session {
  return {
    id: `claude:${title}`,
    engine: "claude",
    uuid: title,
    short_uuid: title,
    cwd: "/x",
    project: "/x",
    last_mtime: 0,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
  };
}
function pageOf(sessions: Session[]): SessionsPage {
  return { sessions, next_offset: null, total: sessions.length, facets: { projects: [], engines: [] } };
}

beforeEach(() => {
  mockSessions.mockReset();
  mockArchive.mockReset();
});

test("a stale (slower, earlier) filter response cannot overwrite the newer query", async () => {
  const mount = deferred();
  const qa = deferred();
  const qab = deferred();
  mockSessions
    .mockReturnValueOnce(mount.promise) // initial load
    .mockReturnValueOnce(qa.promise) // query "a"
    .mockReturnValueOnce(qab.promise); // query "ab"

  const { result } = renderHook(() => useSessionsList());
  await act(async () => {
    mount.resolve(pageOf([]));
  });

  await act(async () => {
    result.current.update({ q: "a" });
  });
  await act(async () => {
    result.current.update({ q: "ab" });
  });

  // The NEWER request ("ab") resolves first, then the stale older one ("a").
  await act(async () => {
    qab.resolve(pageOf([sess("AB")]));
  });
  await act(async () => {
    qa.resolve(pageOf([sess("A-stale")]));
  });

  // The stale "a" response must be dropped — state reflects the current "ab" query.
  await waitFor(() => expect(result.current.sessions.map((s) => s.title)).toEqual(["AB"]));
});

test("archiving a row in a partially loaded list keeps the next unloaded row reachable", async () => {
  // Server active set is [A, B, C]; page 0 loads [A, B] with next_offset 2.
  mockSessions.mockResolvedValueOnce({
    sessions: [sess("A"), sess("B")],
    next_offset: 2,
    total: 3,
    facets: { projects: [], engines: [] },
  });
  mockArchive.mockResolvedValue({ id: "claude:A", archived: true });

  const { result } = renderHook(() => useSessionsList());
  await waitFor(() => expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B"]));

  // Archive A → server set becomes [B, C]; row leaves the view, total drops, and the
  // next-page offset must shift from 2 → 1 (C moved down one slot).
  await act(async () => {
    await result.current.setArchived("claude:A", false);
  });
  expect(result.current.sessions.map((s) => s.title)).toEqual(["B"]);
  expect(result.current.total).toBe(2);
  expect(result.current.hasMore).toBe(true);

  // Load more must request offset 1 (not the stale 2) and reach C — never skip it.
  mockSessions.mockResolvedValueOnce({
    sessions: [sess("C")],
    next_offset: null,
    total: 2,
    facets: { projects: [], engines: [] },
  });
  await act(async () => {
    result.current.loadMore();
  });
  await waitFor(() => expect(result.current.sessions.map((s) => s.title)).toEqual(["B", "C"]));
  expect(mockSessions).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 1 }));
});

test("favoriting a row flips sticky and floats it to the top; unfavoriting clears it (#122)", async () => {
  mockSessions.mockResolvedValueOnce(pageOf([sess("A"), sess("B"), sess("C")]));
  vi.mocked(api.favorite).mockResolvedValue({ id: "claude:C", sticky: true });
  vi.mocked(api.unfavorite).mockResolvedValue({ id: "claude:C", sticky: false });

  const { result } = renderHook(() => useSessionsList());
  await waitFor(() => expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B", "C"]));

  // Favorite C → calls api.favorite, C gets sticky=true and re-sorts to the very top
  // (sticky-first), with the non-sticky rows keeping their relative order (stable sort).
  await act(async () => {
    await result.current.setSticky("claude:C", true);
  });
  expect(api.favorite).toHaveBeenCalledWith("claude:C");
  expect(result.current.sessions.map((s) => s.title)).toEqual(["C", "A", "B"]);
  expect(result.current.sessions.find((s) => s.title === "C")?.sticky).toBe(true);

  // Unfavorite C → calls api.unfavorite, flag clears (server reorders by recency on the
  // next poll; the local re-sort just drops the pin).
  await act(async () => {
    await result.current.setSticky("claude:C", false);
  });
  expect(api.unfavorite).toHaveBeenCalledWith("claude:C");
  expect(result.current.sessions.find((s) => s.title === "C")?.sticky).toBe(false);
});

// ---- #159: live polling + visibility-aware pause + silent failures ----

test("polling refetches from offset 0 every 15s with limit covering the loaded rows (#159)", async () => {
  // Initial page has 2 rows; the next page (loaded via loadMore) brings one more so the
  // refresh must request a limit large enough to cover *all* loaded rows, not just PAGE.
  mockSessions
    .mockResolvedValueOnce({
      sessions: [sess("A"), sess("B")],
      next_offset: 2,
      total: 3,
      facets: { projects: [], engines: [] },
    })
    .mockResolvedValueOnce({
      sessions: [sess("C")],
      next_offset: null,
      total: 3,
      facets: { projects: [], engines: [] },
    });
  vi.useFakeTimers();
  try {
    const { result } = renderHook(() => useSessionsList());
    await vi.waitFor(() => expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B"]));
    await act(async () => {
      result.current.loadMore();
    });
    await vi.waitFor(() =>
      expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B", "C"]),
    );

    mockSessions.mockClear();
    mockSessions.mockResolvedValueOnce({
      // Background refresh — same 3 rows, reordered with C first (new activity).
      sessions: [sess("C"), sess("A"), sess("B")],
      next_offset: null,
      total: 3,
      facets: { projects: [], engines: [] },
    });
    // Advance to the next 15s tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(mockSessions).toHaveBeenCalledTimes(1);
    const call = mockSessions.mock.calls[0]?.[0] ?? {};
    expect(call.offset).toBe(0);
    // The contract is "limit covers all loaded rows" — never drop loaded pages on refresh.
    expect(call.limit ?? 0).toBeGreaterThanOrEqual(3);
    expect(result.current.sessions.map((s) => s.title)).toEqual(["C", "A", "B"]);
  } finally {
    vi.useRealTimers();
  }
});

test("background poll failure preserves rows and does NOT set the error state (#159)", async () => {
  mockSessions.mockResolvedValueOnce(pageOf([sess("A"), sess("B")]));
  vi.useFakeTimers();
  try {
    const { result } = renderHook(() => useSessionsList());
    await vi.waitFor(() => expect(result.current.sessions).toHaveLength(2));

    mockSessions.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B"]); // kept
    expect(result.current.error).toBeNull(); // no UI flicker
    expect(result.current.loading).toBe(false);
  } finally {
    vi.useRealTimers();
  }
});

test("hidden tab pauses polling; becoming visible again triggers an immediate refresh (#159)", async () => {
  mockSessions.mockResolvedValue(pageOf([sess("A")]));
  vi.useFakeTimers();
  let hidden = false;
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  try {
    renderHook(() => useSessionsList());
    await vi.waitFor(() => expect(mockSessions).toHaveBeenCalledTimes(1));

    // Hide the tab → interval stops; advance time → no extra fetch.
    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(mockSessions).toHaveBeenCalledTimes(1);

    // Show the tab again → immediate refresh + interval resumes.
    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(mockSessions).toHaveBeenCalledTimes(2));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(mockSessions).toHaveBeenCalledTimes(3);
  } finally {
    vi.useRealTimers();
  }
});

// Hermes review of #168: polling must NOT start until the initial (non-silent) fetch
// settles. Otherwise a silent 15s poll firing while the initial /api/sessions is still
// pending bumps reqId past the visible request — neither completes setLoading(false)
// (gen-mismatch on the old + opts.silent on the new) → loading stuck `true` → "Load
// more" disabled forever / spinner forever.
test("polling does not start until the initial fetch has settled (#168 race)", async () => {
  // Defer the initial fetch indefinitely so the poller's 15s tick would fire before it
  // resolves. With the gate in place, NO silent poll request goes out.
  let resolveInit!: (v: SessionsPage) => void;
  mockSessions.mockReturnValueOnce(new Promise<SessionsPage>((res) => { resolveInit = res; }));
  vi.useFakeTimers();
  try {
    renderHook(() => useSessionsList());
    // The first call is the bootstrap fetch (still pending).
    expect(mockSessions).toHaveBeenCalledTimes(1);

    // Advance well past the polling interval. With the buggy code this would fire a
    // silent poll mid-bootstrap; with the gate it does NOT.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });
    expect(mockSessions).toHaveBeenCalledTimes(1);

    // Resolve the bootstrap; polling unblocks. Advancing one tick now produces exactly
    // one extra (silent) fetch.
    mockSessions.mockResolvedValueOnce(pageOf([sess("A")]));
    await act(async () => {
      resolveInit(pageOf([sess("A")]));
      await Promise.resolve(); // flush the bootstrap's `finally`
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(mockSessions).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

// Hermes review of PR #168 (round 2): even with the initialLoaded gate, a silent poll
// firing while a non-silent `loadMore` is in flight still strands `loading=true` — the
// poll bumps reqId, the poll skips setLoading(false) because it's silent, and the older
// loadMore's finally is skipped because its generation is stale. Fix: suppress polling
// while ANY visible request is in flight.
test("polling is suppressed while loadMore is in flight (#168 race round 2)", async () => {
  // Bootstrap returns one page with more available.
  mockSessions.mockResolvedValueOnce({
    sessions: [sess("A")],
    next_offset: 1,
    total: 2,
    facets: { projects: [], engines: [] },
  });
  vi.useFakeTimers();
  try {
    const { result } = renderHook(() => useSessionsList());
    await vi.waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.hasMore).toBe(true);
    expect(result.current.loading).toBe(false);

    // Defer the loadMore so the silent poll can interleave.
    let resolveMore!: (v: SessionsPage) => void;
    mockSessions.mockReturnValueOnce(new Promise<SessionsPage>((res) => { resolveMore = res; }));
    await act(async () => {
      result.current.loadMore();
    });
    expect(result.current.loading).toBe(true); // loadMore is now pending

    // 15s passes — the silent poll WOULD fire here in the buggy version, supersede the
    // loadMore by bumping reqId, and leave loading stuck. With the fix, the poll is
    // skipped (visibleInFlight > 0).
    const callsBefore = mockSessions.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(mockSessions.mock.calls.length).toBe(callsBefore); // no silent poll fired

    // Now resolve the loadMore — loading clears, rows append.
    await act(async () => {
      resolveMore({
        sessions: [sess("B")],
        next_offset: null,
        total: 2,
        facets: { projects: [], engines: [] },
      });
      await Promise.resolve();
    });
    expect(result.current.loading).toBe(false); // NOT stuck true
    expect(result.current.sessions.map((s) => s.title)).toEqual(["A", "B"]);
  } finally {
    vi.useRealTimers();
  }
});

test("setProject writes the assignment and folds the new resolution into the row (#424 Phase 5b)", async () => {
  mockSessions.mockResolvedValue(pageOf([{ ...sess("S"), cwd: "/x" }]));
  vi.mocked(api.setSessionProject).mockResolvedValue({ id: "claude:S", project_id: "p-1" });
  const { result } = renderHook(() => useSessionsList());
  await waitFor(() => expect(result.current.sessions).toHaveLength(1));

  // Assign to an entity → PATCH the id, fold the entity ref into the row.
  await act(async () => {
    await result.current.setProject("claude:S", {
      kind: "project",
      id: "p-1",
      name: "Cayoo",
      color: "",
    });
  });
  expect(api.setSessionProject).toHaveBeenCalledWith("claude:S", "p-1");
  expect(result.current.sessions[0].project).toMatchObject({ kind: "project", id: "p-1", name: "Cayoo" });

  // Unassign → PATCH null, row falls back to the folder keyed by its cwd.
  await act(async () => {
    await result.current.setProject("claude:S", null);
  });
  expect(api.setSessionProject).toHaveBeenLastCalledWith("claude:S", null);
  expect(result.current.sessions[0].project).toMatchObject({ kind: "folder", id: "/x" });
});
