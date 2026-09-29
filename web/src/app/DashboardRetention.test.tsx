import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { RecentWork } from "../components/ask/RecentWork";
import { useNeedsYou } from "../components/ask/useNeedsYou";
import { usePolled } from "../components/dashboard/usePolled";
import { api } from "../lib/api";
import type {
  AppConfig,
  NeedsYouPayload,
  RecentWorkPayload,
} from "../types/api";
import { ConfigCtx } from "./config";
import { DashboardRetentionProvider } from "./DashboardRetentionContext";
import {
  useDashboardRetention,
  type DashboardRetention,
} from "./dashboardRetentionStore";

/* #1223 — the dashboard's reads are RETAINED above the router and ALWAYS revalidated.
 *
 * The same contract as the map (#1007): the retained value decides what is drawn first, never
 * whether to fetch. Each case mounts the provider once (the shell) and mounts / unmounts a
 * consumer beneath it (the route), which is exactly what navigating away and back does. */

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      needsYou: vi.fn(),
      recentWork: vi.fn(),
      refreshRecentWork: vi.fn(),
    },
  };
});
const mockNeedsYou = vi.mocked(api.needsYou);
const mockRecent = vi.mocked(api.recentWork);
const mockRefreshRecent = vi.mocked(api.refreshRecentWork);

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** The hard scope, as a raw config value — `ConfigProvider` would fetch. `null` = not loaded. */
function Shell({
  exclusions = [],
  loaded = true,
  children,
}: {
  exclusions?: string[];
  loaded?: boolean;
  children: ReactNode;
}) {
  const cfg = loaded
    ? ({
        project_roots: [],
        folder_exclusions: exclusions,
      } as unknown as AppConfig)
    : null;
  return (
    <MemoryRouter>
      <ConfigCtx.Provider value={cfg}>
        <DashboardRetentionProvider>{children}</DashboardRetentionProvider>
      </ConfigCtx.Provider>
    </MemoryRouter>
  );
}

function Tile({ fetcher }: { fetcher: () => Promise<string> }) {
  const [res, retry, refreshing] = usePolled("tile", fetcher, 60_000);
  return (
    <>
      <div data-testid="state">
        {res.status === "ok"
          ? `ok:${res.data}${res.refreshFailed ? ":failed" : ""}`
          : res.status}
      </div>
      <div data-testid="refreshing">{String(refreshing)}</div>
      <button onClick={() => void retry()}>retry</button>
    </>
  );
}

const state = () => screen.getByTestId("state").textContent;
const refreshing = () => screen.getByTestId("refreshing").textContent;

beforeEach(() => {
  mockNeedsYou.mockReset();
  mockRecent.mockReset();
  mockRefreshRecent.mockReset();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("usePolled retention (#1223)", () => {
  test("coming back paints the last read at once, and still re-reads behind it", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("first");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    // First visit: nothing retained → cold, and a cold read is not "refreshing".
    expect(state()).toBe("loading");
    await waitFor(() => expect(state()).toBe("ok:first"));

    rerender(<Shell>{null}</Shell>); // navigate away
    const back = deferred<string>();
    fetcher.mockReturnValueOnce(back.promise);
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    // Painted immediately from retention, with the re-read in flight behind it.
    expect(state()).toBe("ok:first");
    expect(refreshing()).toBe("true");
    expect(fetcher).toHaveBeenCalledTimes(2);

    await act(async () => back.resolve("second"));
    expect(state()).toBe("ok:second");
    expect(refreshing()).toBe("false");
  });

  test("a failed re-read keeps the retained data and marks it", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("kept");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:kept"));
    rerender(<Shell>{null}</Shell>);
    fetcher.mockRejectedValueOnce(new Error("down"));
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:kept:failed"));
    expect(refreshing()).toBe("false");
  });

  test("a successful EMPTY read is retained as empty, never as loading", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:"));
    rerender(<Shell>{null}</Shell>);
    fetcher.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    expect(state()).toBe("ok:");
  });

  test("an old request neither commits nor clears a newer request's flag", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("base");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:base"));
    rerender(<Shell>{null}</Shell>);

    const slow = deferred<string>();
    const fresh = deferred<string>();
    fetcher
      .mockReturnValueOnce(slow.promise)
      .mockReturnValueOnce(fresh.promise);
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await userEvent.click(screen.getByText("retry")); // supersedes the mount read

    await act(async () => slow.resolve("stale"));
    expect(state()).toBe("ok:base"); // the superseded answer did not paint…
    expect(refreshing()).toBe("true"); // …nor clear the newer read's flag

    await act(async () => fresh.resolve("fresh"));
    expect(state()).toBe("ok:fresh");
    expect(refreshing()).toBe("false");

    // …and the superseded answer was not retained either.
    rerender(<Shell>{null}</Shell>);
    fetcher.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    expect(state()).toBe("ok:fresh");
  });

  test("a read that lands after leaving is not retained", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("seen");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:seen"));
    rerender(<Shell>{null}</Shell>);
    const late = deferred<string>();
    fetcher.mockReturnValueOnce(late.promise);
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    rerender(<Shell>{null}</Shell>); // leave again while it is in flight
    await act(async () => late.resolve("late"));
    fetcher.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    expect(state()).toBe("ok:seen");
  });

  test("another scope's retained value is never painted", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("scope-a");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:scope-a"));
    rerender(<Shell exclusions={["/x"]}>{null}</Shell>);
    fetcher.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell exclusions={["/x"]}>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    expect(state()).toBe("loading");
  });

  test("a scope change while mounted drops what is painted and the in-flight read", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("scope-a");
    const { rerender } = render(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:scope-a"));

    const oldScope = deferred<string>();
    const newScope = deferred<string>();
    fetcher.mockReturnValueOnce(oldScope.promise);
    await userEvent.click(screen.getByText("retry")); // a read under scope A…
    fetcher.mockReturnValueOnce(newScope.promise);
    rerender(
      <Shell exclusions={["/x"]}>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    // …the scope moves: cold, not "refreshing" another boundary's rows.
    expect(state()).toBe("loading");
    await act(async () => oldScope.resolve("scope-a-late"));
    expect(state()).toBe("loading");
    await act(async () => newScope.resolve("scope-b"));
    expect(state()).toBe("ok:scope-b");
  });

  test("config answering is not a scope change: the first read is kept and retained", async () => {
    const first = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(first.promise);
    const { rerender } = render(
      <Shell loaded={false}>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    await act(async () => first.resolve("one"));
    expect(state()).toBe("ok:one");
    expect(fetcher).toHaveBeenCalledTimes(1);
    rerender(<Shell>{null}</Shell>);
    fetcher.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <Tile fetcher={fetcher} />
      </Shell>,
    );
    expect(state()).toBe("ok:one");
  });
});

function needs(ids: string[], totalUnfiltered: number): NeedsYouPayload {
  return {
    rows: ids.map((id) => ({ id })),
    total: ids.length,
    total_unfiltered: totalUnfiltered,
    needs_you_ids: ["a", "b", "c"].slice(0, totalUnfiltered),
    facets: { engines: [], projects: [] },
  } as unknown as NeedsYouPayload;
}

function NeedsProbe({ engine }: { engine: string }) {
  const { state: st, membership, refreshing: r } = useNeedsYou(1, engine, "");
  return (
    <>
      <div data-testid="state">
        {st.status}:{st.data ? st.data.total : "-"}
      </div>
      <div data-testid="count">{membership ? membership.total : "-"}</div>
      <div data-testid="refreshing">{String(r)}</div>
    </>
  );
}

describe("useNeedsYou retention (#1223)", () => {
  test("a filtered list comes back with the UNFILTERED count, both from one commit", async () => {
    mockNeedsYou.mockResolvedValueOnce(needs(["a"], 3));
    const { rerender } = render(
      <Shell>
        <NeedsProbe engine="claude" />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:1"));
    expect(screen.getByTestId("count").textContent).toBe("3");

    rerender(<Shell>{null}</Shell>);
    mockNeedsYou.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <NeedsProbe engine="claude" />
      </Shell>,
    );
    expect(state()).toBe("ok:1");
    expect(screen.getByTestId("count").textContent).toBe("3");
    expect(refreshing()).toBe("true");
  });

  test("a restored list comes back with ITS OWN count, never a newer read's (review 5407)", async () => {
    // Unfiltered: one row, one needs you.
    mockNeedsYou.mockResolvedValueOnce(needs(["a"], 1));
    const { rerender } = render(
      <Shell>
        <NeedsProbe engine="" />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:1"));
    expect(screen.getByTestId("count").textContent).toBe("1");
    // Filtered, after that session stopped needing you: no rows, count 0.
    mockNeedsYou.mockResolvedValueOnce(needs([], 0));
    rerender(
      <Shell>
        <NeedsProbe engine="claude" />
      </Shell>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("count").textContent).toBe("0"),
    );
    // Clear the filter with its read held: the retained one-row list must come back with the
    // count that was read WITH it — a one-row list beside "0" is a contradiction on screen.
    mockNeedsYou.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <NeedsProbe engine="" />
      </Shell>,
    );
    expect(state()).toBe("ok:1");
    expect(screen.getByTestId("count").textContent).toBe("1");
  });

  test("a question never asked before is cold, not another question's answer", async () => {
    mockNeedsYou.mockResolvedValueOnce(needs(["a"], 3));
    const { rerender } = render(
      <Shell>
        <NeedsProbe engine="claude" />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:1"));
    mockNeedsYou.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <NeedsProbe engine="codex" />
      </Shell>,
    );
    expect(state()).toBe("loading:-");
  });

  test("an empty list is retained as empty", async () => {
    mockNeedsYou.mockResolvedValueOnce(needs([], 0));
    const { rerender } = render(
      <Shell>
        <NeedsProbe engine="" />
      </Shell>,
    );
    await waitFor(() => expect(state()).toBe("ok:0"));
    rerender(<Shell>{null}</Shell>);
    mockNeedsYou.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <NeedsProbe engine="" />
      </Shell>,
    );
    expect(state()).toBe("ok:0");
  });
});

function recent(summary: string, stale = false): RecentWorkPayload {
  return {
    entries: [
      {
        session_key: "claude:x",
        ts: 1_700_000_000,
        text: summary,
        title: "t",
        engine: "claude",
        project: { id: "p", name: "P" },
        session_recap: "",
      },
    ],
    stale,
    configured: true,
    source: "ai",
    generated_at: 1_700_000_000,
  } as unknown as RecentWorkPayload;
}

describe("RecentWork retention (#1223)", () => {
  test("coming back paints the last snapshot; the bar tracks the read, not the AI refresh", async () => {
    const onRevalidating = vi.fn();
    mockRecent.mockResolvedValueOnce(recent("first summary"));
    const { rerender } = render(
      <Shell>
        <RecentWork
          windowDays={1}
          onWindowDays={() => {}}
          onRevalidating={onRevalidating}
        />
      </Shell>,
    );
    await screen.findByText(/first summary/);
    rerender(<Shell>{null}</Shell>);

    const read = deferred<RecentWorkPayload>();
    const ai = deferred<RecentWorkPayload>();
    mockRecent.mockReturnValueOnce(read.promise);
    mockRefreshRecent.mockReturnValueOnce(ai.promise);
    onRevalidating.mockClear();
    rerender(
      <Shell>
        <RecentWork
          windowDays={1}
          onWindowDays={() => {}}
          onRevalidating={onRevalidating}
        />
      </Shell>,
    );
    expect(screen.getByText(/first summary/)).toBeTruthy();
    expect(onRevalidating).toHaveBeenLastCalledWith(true);

    // The snapshot answers stale → the AI refresh starts; the bar is already done.
    await act(async () => read.resolve(recent("second summary", true)));
    expect(onRevalidating).toHaveBeenLastCalledWith(false);
    expect(mockRefreshRecent).toHaveBeenCalledTimes(1);
    await act(async () => ai.resolve(recent("third summary")));
    expect(screen.getByText(/third summary/)).toBeTruthy();
  });

  test("an older read resolving after a newer visit's never replaces it (review 5407)", async () => {
    const a = deferred<RecentWorkPayload>();
    mockRecent.mockReturnValueOnce(a.promise);
    const { rerender } = render(
      <Shell>
        <RecentWork windowDays={1} onWindowDays={() => {}} />
      </Shell>,
    );
    rerender(<Shell>{null}</Shell>); // leave while A is in flight
    mockRecent.mockResolvedValueOnce(recent("newer summary B"));
    rerender(
      <Shell>
        <RecentWork windowDays={1} onWindowDays={() => {}} />
      </Shell>,
    );
    await screen.findByText(/newer summary B/);
    rerender(<Shell>{null}</Shell>);
    await act(async () => a.resolve(recent("older summary A")));

    mockRecent.mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <Shell>
        <RecentWork windowDays={1} onWindowDays={() => {}} />
      </Shell>,
    );
    expect(screen.getByText(/newer summary B/)).toBeTruthy();
    expect(screen.queryByText(/older summary A/)).toBeNull();
  });

  test("a failed re-read over a painted window says so and keeps it", async () => {
    mockRecent.mockResolvedValueOnce(recent("kept summary"));
    const { rerender } = render(
      <Shell>
        <RecentWork windowDays={1} onWindowDays={() => {}} />
      </Shell>,
    );
    await screen.findByText(/kept summary/);
    rerender(<Shell>{null}</Shell>);
    mockRecent.mockRejectedValueOnce(new Error("down"));
    rerender(
      <Shell>
        <RecentWork windowDays={1} onWindowDays={() => {}} />
      </Shell>,
    );
    await screen.findByTestId("recent-work-refresh-error");
    expect(screen.getByText(/kept summary/)).toBeTruthy();
    expect(screen.queryByTestId("recent-work-error")).toBeNull();
  });
});

describe("DashboardRetentionProvider (#1223)", () => {
  /** The provider's own fence, independent of any hook's generation guard: a consumer that commits
   *  a read taken under a scope that has since moved must not retain it under the new one. */
  test("a write stamped with a scope that has moved is dropped", async () => {
    const grabbed: { store: DashboardRetention | null } = { store: null };
    function Grab() {
      const s = useDashboardRetention();
      useEffect(() => {
        grabbed.store = s;
      });
      return null;
    }
    const { rerender } = render(
      <Shell>
        <Grab />
      </Shell>,
    );
    const scopeA = grabbed.store!.scopeKey;
    rerender(
      <Shell exclusions={["/x"]}>
        <Grab />
      </Shell>,
    );
    act(() => grabbed.store!.write("k", "from-a", scopeA));
    expect(grabbed.store!.read("k")).toBeUndefined();
    act(() => grabbed.store!.write("k", "from-b", grabbed.store!.scopeKey));
    expect(grabbed.store!.read("k")).toBe("from-b");
  });
});
