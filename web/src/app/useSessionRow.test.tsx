import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../lib/api";
import type { Session } from "../types/api";
import { SessionsProvider } from "./SessionsContext";
import { isNewSessionPlaceholder, useSessionsStore } from "./sessionsStore";
import { useSessionRow } from "./useSessionRow";

/** The pane's own row resolution (#867).
 *
 *  The sidebar's list is one filtered, scope-stripped 20-row page, so it can only ever name a
 *  small slice of what a URL can address. These pin the four things that go wrong once a pane
 *  fetches for itself: asking twice for one pane, asking for a key that is about to die, a
 *  response landing on the wrong pane after a navigation, and a fetched copy outliving the
 *  live list row it should defer to.
 */

function row(id: string, over: Partial<Session> = {}): Session {
  const [engine, uuid] = id.split(":");
  return {
    id,
    engine,
    uuid,
    short_uuid: uuid.slice(0, 8),
    cwd: `/home/u/${uuid}`,
    project: { kind: "folder", id: `/home/u/${uuid}`, name: `/home/u/${uuid}` },
    last_mtime: 1,
    title: `title-${uuid}`,
    ...over,
  } as unknown as Session;
}

/** Two consumers of ONE pane, mirroring SessionView + Terminal. */
function Pane({ k, fallback }: { k: string; fallback?: string }) {
  const a = useSessionRow(k, fallback);
  const b = useSessionRow(k, fallback);
  return (
    <>
      <span data-testid="a">{a?.title ?? "—"}</span>
      <span data-testid="b">{b?.title ?? "—"}</span>
      <span data-testid="cwd">{a?.cwd ?? "—"}</span>
      {/* "none" = nothing was accepted; anything else is what leaked through. A list page has
          no `project`, so it prints "undefined" — which is how this probe separates the guard
          from the failure it exists to stop. */}
      <span data-testid="kind">
        {a === undefined ? "none" : String(a.project?.kind)}
      </span>
    </>
  );
}

/** Publishes rows into the store on mount, the way the sidebar does. */
function Seed({ rows }: { rows: Session[] }) {
  const { setSessions } = useSessionsStore();
  useEffect(() => setSessions(rows), [rows, setSessions]);
  return null;
}

/** The sidebar's 15 s poll, on demand: click to publish the row it finally loaded. */
function LateList() {
  const { setSessions } = useSessionsStore();
  return (
    <button
      data-testid="publish"
      onClick={() => setSessions([row("claude:ccc", { title: "from-list" })])}
    />
  );
}

afterEach(() => vi.restoreAllMocks());

test("a row the sidebar already has is used as-is — no request at all", async () => {
  const spy = vi.spyOn(api, "session");
  const listed = row("claude:aaa");
  // The list lands BEFORE the pane mounts — i.e. you tapped the row in the sidebar, which is
  // how most sessions are opened. Seeding it in the same commit as the pane would model a
  // different thing (a cold load whose list happens to arrive in the same tick), and there the
  // pane does fetch: it cannot know a row is one tick away, and waiting would leave every deep
  // link — the case this whole hook exists for — nameless for as long as the list takes.
  const { rerender } = render(
    <SessionsProvider>
      <Seed rows={[listed]} />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.queryByTestId("a")).toBeNull());
  rerender(
    <SessionsProvider>
      <Seed rows={[listed]} />
      <Pane k="claude:aaa" />
    </SessionsProvider>,
  );
  await waitFor(() =>
    expect(screen.getByTestId("a")).toHaveTextContent("title-aaa"),
  );
  expect(spy).not.toHaveBeenCalled();
});

test("a row the sidebar does NOT have is fetched exactly once for the whole pane", async () => {
  const spy = vi.spyOn(api, "session").mockResolvedValue(row("claude:bbb"));
  render(
    <SessionsProvider>
      <Pane k="claude:bbb" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-bbb"));
  // Both consumers show it, and it cost ONE request — the guard lives in the provider, so a
  // sibling asking in the same commit joins rather than starting a second.
  expect(screen.getByTestId("b")).toHaveTextContent("title-bbb");
  expect(spy).toHaveBeenCalledTimes(1);
  expect(spy).toHaveBeenCalledWith("claude:bbb");
});

test("an unreconciled new-session placeholder is never requested (#127)", async () => {
  const spy = vi.spyOn(api, "session");
  render(
    <SessionsProvider>
      <Pane k="opencode:new-11111111-1111-1111-1111-111111111111" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("—"));
  // `canonical_key` rejects the shape server-side, so asking would cache a 404 under a key the
  // converge is about to replace — and that 404 would stand as the pane's answer.
  expect(spy).not.toHaveBeenCalled();
  expect(isNewSessionPlaceholder("opencode:new-11111111-1111-1111-1111-111111111111")).toBe(true);
  expect(isNewSessionPlaceholder("opencode:ses_abc")).toBe(false);
});

test("after the converge the REAL key is fetched, once", async () => {
  const spy = vi.spyOn(api, "session").mockResolvedValue(row("opencode:ses_real"));
  const ph = "opencode:new-11111111-1111-1111-1111-111111111111";
  const { rerender } = render(
    <SessionsProvider>
      <Pane k={ph} />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("—"));
  expect(spy).not.toHaveBeenCalled();
  // `onReconcileId` has run: the terminal identity stays frozen on the placeholder, the URL is
  // now the real id. The row exists only under that real id, so THAT is what we ask for.
  rerender(
    <SessionsProvider>
      <Pane k={ph} fallback="opencode:ses_real" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-ses_real"));
  expect(spy).toHaveBeenCalledTimes(1);
  expect(spy).toHaveBeenCalledWith("opencode:ses_real");
});

test("a response that lands after a navigation cannot surface on the pane that replaced it", async () => {
  // A resolves LAST, long after the pane moved to B. Every outcome is stored under the key it
  // was requested for, so A has no slot to land in on B's pane.
  let resolveA: (s: Session) => void = () => {};
  const spy = vi.spyOn(api, "session").mockImplementation((id: string) => {
    if (id === "claude:A") return new Promise<Session>((r) => (resolveA = r));
    return Promise.resolve(row("claude:B"));
  });

  const { rerender } = render(
    <SessionsProvider>
      <Pane k="claude:A" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalledWith("claude:A"));
  expect(screen.getByTestId("a")).toHaveTextContent("—"); // A still in flight

  rerender(
    <SessionsProvider>
      <Pane k="claude:B" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-B"));

  resolveA(row("claude:A"));
  await new Promise((r) => setTimeout(r, 0));
  // B's pane is untouched by A's late answer — no title swap, no cwd swap.
  expect(screen.getByTestId("a")).toHaveTextContent("title-B");
  expect(screen.getByTestId("cwd")).toHaveTextContent("/home/u/B");
});

test("A → B → A serves each pane its own row", async () => {
  vi.spyOn(api, "session").mockImplementation((id: string) =>
    Promise.resolve(row(id)),
  );
  const { rerender } = render(
    <SessionsProvider>
      <Pane k="claude:A" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-A"));
  rerender(
    <SessionsProvider>
      <Pane k="claude:B" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-B"));
  rerender(
    <SessionsProvider>
      <Pane k="claude:A" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("title-A"));
});

test("a 404 settles the key — it degrades that pane only, and is not retried", async () => {
  const spy = vi.spyOn(api, "session").mockRejectedValue(new Error("404"));
  const { rerender } = render(
    <SessionsProvider>
      <Pane k="claude:gone" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  expect(screen.getByTestId("a")).toHaveTextContent("—");
  rerender(
    <SessionsProvider>
      <Pane k="claude:gone" />
    </SessionsProvider>,
  );
  await new Promise((r) => setTimeout(r, 0));
  expect(spy).toHaveBeenCalledTimes(1); // settled, not pending
});

test("the list supersedes a fetched copy when the poll finally brings the row", async () => {
  vi.spyOn(api, "session").mockResolvedValue(row("claude:ccc", { title: "fetched" }));
  function Harness() {
    return (
      <SessionsProvider>
        <LateList />
        <Pane k="claude:ccc" />
      </SessionsProvider>
    );
  }
  render(<Harness />);
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("fetched"));
  // The 15 s poll lands the row. The list is the fresher source — `working`, `last_mtime` and
  // the review fields move on it — so it must win over the snapshot we fetched.
  screen.getByTestId("publish").click();
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("from-list"));
});

test("a response that is not the row we asked for is a miss, not a row", async () => {
  // The shape that actually happened: a `**/api/sessions**` route in the e2e suite also matches
  // `/api/sessions/<id>`, so the lookup was handed the LIST page. Without this check that object
  // reaches `row.project.kind` in the pane header, throws, and the route's error boundary
  // replaces the whole live session with "we couldn't load this part of the app".
  const spy = vi
    .spyOn(api, "session")
    .mockResolvedValue({ sessions: [row("claude:ddd")], total: 1 } as unknown as Session);
  render(
    <SessionsProvider>
      <Pane k="claude:ddd" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  // Flush the resolution — asserting before it lands would pass against no guard at all.
  await act(async () => {
    await Promise.resolve();
  });
  // "none", not "undefined": the object was refused, not accepted-and-missing-a-project.
  expect(screen.getByTestId("kind")).toHaveTextContent("none");
});

test("a row for a DIFFERENT session is refused", async () => {
  // Same guard, the other way round: whatever answered gave us someone else's row. Rendering it
  // would put another session's project and folder on this pane's header.
  const spy = vi.spyOn(api, "session").mockResolvedValue(row("claude:someone-else"));
  render(
    <SessionsProvider>
      <Pane k="claude:eee" />
    </SessionsProvider>,
  );
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByTestId("a")).toHaveTextContent("—");
  expect(screen.getByTestId("cwd")).toHaveTextContent("—");
  expect(screen.getByTestId("kind")).toHaveTextContent("none");
});

test("a transient failure is retried, not settled as a permanent 404 (#867 review)", async () => {
  // `api.session` rejects for network errors and every non-2xx alike. Settling those as null
  // left a hidden/archived pane nameless until the whole provider remounted — and the filtered
  // list is precisely what cannot rescue those rows.
  vi.useFakeTimers();
  try {
    const spy = vi
      .spyOn(api, "session")
      .mockRejectedValueOnce(new ApiError(500, "boom"))
      .mockResolvedValue(row("claude:flaky"));
    render(
      <SessionsProvider>
        <Pane k="claude:flaky" />
      </SessionsProvider>,
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(spy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("a")).toHaveTextContent("title-flaky");
  } finally {
    vi.useRealTimers();
  }
});

test("a 404 is settled immediately — never retried", async () => {
  vi.useFakeTimers();
  try {
    const spy = vi.spyOn(api, "session").mockRejectedValue(new ApiError(404, "gone"));
    render(
      <SessionsProvider>
        <Pane k="claude:missing" />
      </SessionsProvider>,
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    // The id is unknown, hidden behind the hard scope, or gone. That IS the answer.
    expect(spy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("kind")).toHaveTextContent("none");
  } finally {
    vi.useRealTimers();
  }
});

test("a transient failure that exhausts its retries releases the guard", async () => {
  // Out of attempts must not mean "settled false". Releasing the single-flight guard is what
  // lets simply navigating back to the session try again.
  vi.useFakeTimers();
  try {
    const spy = vi.spyOn(api, "session").mockRejectedValue(new ApiError(503, "down"));
    const { rerender } = render(
      <SessionsProvider>
        <Pane k="claude:down" />
      </SessionsProvider>,
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    const afterRetries = spy.mock.calls.length;
    expect(afterRetries).toBeGreaterThan(1); // it retried
    spy.mockResolvedValue(row("claude:down"));
    // Same provider, the pane re-mounts the consumer — the guard must let it through.
    rerender(
      <SessionsProvider>
        <Pane k="claude:other" />
      </SessionsProvider>,
    );
    rerender(
      <SessionsProvider>
        <Pane k="claude:down" />
      </SessionsProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(spy.mock.calls.length).toBeGreaterThan(afterRetries);
  } finally {
    vi.useRealTimers();
  }
});

test("a MOUNTED pane recovers on its own once the outage ends (#867 review r4)", async () => {
  // Releasing the single-flight guard is a ref write, and a ref write is invisible to React: a
  // pane that never navigates would re-run nothing and stay nameless for the whole outage. The
  // provider therefore bumps a generation, which is in this hook's effect deps.
  vi.useFakeTimers();
  try {
    const spy = vi.spyOn(api, "session").mockRejectedValue(new ApiError(503, "down"));
    render(
      <SessionsProvider>
        <Pane k="claude:outage" />
      </SessionsProvider>,
    );
    // Burn the fast budget without ever unmounting or navigating.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    const during = spy.mock.calls.length;
    expect(during).toBeGreaterThan(1);

    spy.mockResolvedValue(row("claude:outage"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    // No navigation, no remount — the pane healed itself.
    expect(spy.mock.calls.length).toBeGreaterThan(during);
    expect(screen.getByTestId("a")).toHaveTextContent("title-outage");
  } finally {
    vi.useRealTimers();
  }
});

test("a 404 is settled, but revalidated later — a session can appear under that id", async () => {
  vi.useFakeTimers();
  try {
    const spy = vi.spyOn(api, "session").mockRejectedValue(new ApiError(404, "gone"));
    render(
      <SessionsProvider>
        <Pane k="claude:later" />
      </SessionsProvider>,
    );
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    // Settled: no retry inside the fast budget.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(spy).toHaveBeenCalledTimes(1);
    // …but not for life. A deep link can land before the transcript exists at all.
    spy.mockResolvedValue(row("claude:later"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(screen.getByTestId("a")).toHaveTextContent("title-later");
  } finally {
    vi.useRealTimers();
  }
});

test("a mismatched 2xx is retried, and the pane recovers when the right row arrives", async () => {
  // The guard refusing the wrong payload is only half the contract. Settling it as `null` left
  // the key in `asked` — every release/revalidate path lives in the rejection branch — so one
  // misrouted 2xx pinned the pane empty for the provider's whole life. The earlier tests proved
  // the wrong row is not RENDERED; this one proves the pane RECOVERS.
  vi.useFakeTimers();
  try {
    const spy = vi
      .spyOn(api, "session")
      .mockResolvedValue({ sessions: [row("claude:mix")], total: 1 } as unknown as Session);
    render(
      <SessionsProvider>
        <Pane k="claude:mix" />
      </SessionsProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    // Refused, and retried rather than settled.
    expect(screen.getByTestId("kind")).toHaveTextContent("none");
    const during = spy.mock.calls.length;
    expect(during).toBeGreaterThan(1);

    spy.mockResolvedValue(row("claude:mix"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(screen.getByTestId("a")).toHaveTextContent("title-mix");
  } finally {
    vi.useRealTimers();
  }
});

test("a row that leaves the list does not fall back to a stale snapshot (#867 review r6)", async () => {
  // A1 is fetched, then a FRESHER A2 arrives via the list, then the list drops the row (a filter,
  // page or visibility change) while the pane stays open. Falling back to A1 would show
  // arbitrarily old project/review metadata — and for a hidden or archived session it could never
  // be refreshed, because list polling cannot reach those rows at all.
  const a1 = row("claude:drift", { title: "stale-A1", cwd: "/old" });
  const a2 = row("claude:drift", { title: "fresh-A2", cwd: "/new" });
  const a3 = row("claude:drift", { title: "refetched-A3", cwd: "/newest" });
  const spy = vi.spyOn(api, "session").mockResolvedValue(a1);

  function Harness({ rows }: { rows: Session[] }) {
    return (
      <SessionsProvider>
        <Seed rows={rows} />
        <Pane k="claude:drift" />
      </SessionsProvider>
    );
  }
  const { rerender } = render(<Harness rows={[]} />);
  await waitFor(() =>
    expect(screen.getByTestId("a")).toHaveTextContent("stale-A1"),
  );

  // The list becomes authoritative with a fresher row.
  rerender(<Harness rows={[a2]} />);
  await waitFor(() =>
    expect(screen.getByTestId("a")).toHaveTextContent("fresh-A2"),
  );

  // …and then drops it. The pane must NOT revert to A1.
  spy.mockResolvedValue(a3);
  rerender(<Harness rows={[]} />);
  await waitFor(() => {
    expect(screen.getByTestId("a")).not.toHaveTextContent("stale-A1");
  });
  expect(screen.getByTestId("cwd")).not.toHaveTextContent("/old");
});

test("a lookup that lands after the list published a fresher row is dropped (#867 r7)", async () => {
  // The ordering that defeats `remember` alone: the request starts while the row is absent, the
  // LIST publishes a fresher A2 while it is still in flight, then the stale A1 resolves. The
  // list keeps winning while it is authoritative, so the overwrite is invisible — until the list
  // drops the row and A1 resurfaces. `SessionView` feeds the file panel's cwd from this same
  // row, so a resurrected snapshot can move the folder being browsed.
  let resolveA1: (s: Session) => void = () => {};
  const a1 = row("claude:order", { title: "stale-A1", cwd: "/old" });
  const a2 = row("claude:order", { title: "fresh-A2", cwd: "/new" });
  vi.spyOn(api, "session").mockImplementation(
    () => new Promise<Session>((r) => (resolveA1 = r)),
  );

  function Harness({ rows }: { rows: Session[] }) {
    return (
      <SessionsProvider>
        <Seed rows={rows} />
        <Pane k="claude:order" />
      </SessionsProvider>
    );
  }
  const { rerender } = render(<Harness rows={[]} />);
  await waitFor(() => expect(api.session).toHaveBeenCalledWith("claude:order"));

  rerender(<Harness rows={[a2]} />); // the list publishes the fresher row…
  await waitFor(() =>
    expect(screen.getByTestId("a")).toHaveTextContent("fresh-A2"),
  );

  resolveA1(a1); // …and only now does the stale request land
  await act(async () => {
    await Promise.resolve();
  });

  rerender(<Harness rows={[]} />); // the list drops it — A1 must NOT be underneath
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByTestId("a")).not.toHaveTextContent("stale-A1");
  expect(screen.getByTestId("cwd")).not.toHaveTextContent("/old");
});

test("navigating listed A → unlisted B issues ONE request for B (#867 r7)", async () => {
  // `listedRef` as a bare boolean was not tied to the identity that set it: on this navigation
  // B's own effect read "the row left the list" and called `forget(B)`, releasing a key whose
  // first request was still in flight. The retry-generation render then started another. Three
  // requests for one pane, whose outcomes could race — a late 404 replacing a good row.
  const a = row("claude:AA", { title: "listed-A" });
  const spy = vi.spyOn(api, "session").mockResolvedValue(row("claude:BB", { title: "row-B" }));

  function Harness({ k, rows }: { k: string; rows: Session[] }) {
    return (
      <SessionsProvider>
        <Seed rows={rows} />
        <Pane k={k} />
      </SessionsProvider>
    );
  }
  const { rerender } = render(<Harness k="claude:AA" rows={[a]} />);
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("listed-A"));
  // The cold-load fetch for A is expected (the list arrives one commit later — see the first
  // test in this file). Clear it so the count below is B's alone, which is what is at issue.
  spy.mockClear();

  rerender(<Harness k="claude:BB" rows={[a]} />); // B is not in the list
  await waitFor(() => expect(screen.getByTestId("a")).toHaveTextContent("row-B"));
  await act(async () => {
    await Promise.resolve();
  });
  expect(spy).toHaveBeenCalledTimes(1);
  expect(spy).toHaveBeenCalledWith("claude:BB");
});

test("a mismatch retried past a list write cannot overwrite it (#867 r8)", async () => {
  // The ordering the first fence missed: the mismatch path was exempted from it so its retry
  // policy could run, and the retry then claimed a BRAND-NEW generation — making it "newer than
  // the list" even though it existed only because of an older request. Superseded is superseded.
  vi.useFakeTimers();
  try {
    const fresh = row("claude:mixord", { title: "fresh-list", cwd: "/new" });
    const stale = row("claude:mixord", { title: "stale-retry", cwd: "/old" });
    let call = 0;
    vi.spyOn(api, "session").mockImplementation(() => {
      call += 1;
      // 1: a mismatched 2xx (someone else's row). 2: the retry, carrying a STALE row.
      // 3+: PENDING forever — losing list authority re-asks, and letting that resolve would
      // mask the thing under test. With it pending, what the pane renders after the list drops
      // the row IS the cached snapshot: `fresh-list` if the superseded retry was dropped,
      // `stale-retry` if it overwrote the list's row.
      if (call === 1) return Promise.resolve(row("claude:someone-else"));
      if (call === 2) return Promise.resolve(stale);
      return new Promise<Session>(() => {});
    });

    function Harness({ rows }: { rows: Session[] }) {
      return (
        <SessionsProvider>
          <Seed rows={rows} />
          <Pane k="claude:mixord" />
        </SessionsProvider>
      );
    }
    const { rerender } = render(<Harness rows={[]} />);
    await act(async () => {
      await Promise.resolve();
    });
    // The list publishes a fresher row while the mismatch retry is pending.
    rerender(<Harness rows={[fresh]} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(screen.getByTestId("a")).toHaveTextContent("fresh-list");

    // …and when the list drops it, the cached snapshot underneath must still be the list's row.
    rerender(<Harness rows={[]} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(screen.getByTestId("a")).toHaveTextContent("fresh-list");
    expect(screen.getByTestId("cwd")).toHaveTextContent("/new");
  } finally {
    vi.useRealTimers();
  }
});
