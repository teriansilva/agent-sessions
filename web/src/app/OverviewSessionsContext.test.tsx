import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import { useOverviewSessions } from "../hooks/useOverviewSessions";
import type { AppConfig, Session, SessionsPage } from "../types/api";
import { ConfigCtx } from "./config";
import { OverviewSessionsProvider } from "./OverviewSessionsContext";
import { useOverviewSessionsStore } from "./overviewSessionsStore";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, sessions: vi.fn() } };
});

const mockSessions = vi.mocked(api.sessions);

function sess(title: string): Session {
  return {
    id: `claude:${title}`,
    engine: "claude",
    uuid: title,
    short_uuid: title,
    cwd: "/x",
    project: { kind: "folder", id: "/x", name: "x" },
    last_mtime: 0,
    first_user_message: "",
    title,
    sticky: false,
    archived: false,
  };
}

function pageOf(sessions: Session[], next: number | null = null): SessionsPage {
  return {
    sessions,
    next_offset: next,
    total: sessions.length,
    facets: { projects: [], engines: [] },
  };
}

beforeEach(() => {
  mockSessions.mockReset();
});
afterEach(() => {
  vi.restoreAllMocks();
});

/** The HARD scope the provider reads. Supplied as a raw context value rather than by mounting
 *  `ConfigProvider`, which would fetch `/api/config` on mount. Only the scope-bearing fields
 *  matter to `scopeKeyOf`. */
function Scope({
  exclusions = [],
  children,
}: {
  exclusions?: string[];
  children: ReactNode;
}) {
  const cfg = {
    project_roots: [],
    folder_exclusions: exclusions,
  } as unknown as AppConfig;
  return (
    <ConfigCtx.Provider value={cfg}>
      <OverviewSessionsProvider>{children}</OverviewSessionsProvider>
    </ConfigCtx.Provider>
  );
}

/** The map route: a CONSUMER of the retained result, mounted and unmounted by navigation while
 *  the provider above it stays put. */
function MapRoute() {
  const { sessions, loading, error, partial, refetch } = useOverviewSessions();
  return (
    <>
      <div data-testid="titles">{sessions.map((s) => s.title).join(",")}</div>
      <div data-testid="loading">{String(loading)}</div>
      <div data-testid="partial">{String(partial)}</div>
      <div data-testid="error">{error ?? ""}</div>
      <button onClick={refetch}>refetch</button>
    </>
  );
}

/** Mirrors the real tree: the provider is the shell, the route comes and goes beneath it. */
function Shell({
  open,
  exclusions = [],
}: {
  open: boolean;
  exclusions?: string[];
}) {
  return (
    <Scope exclusions={exclusions}>
      {open ? <MapRoute /> : <div data-testid="away" />}
    </Scope>
  );
}

const titles = () => screen.getByTestId("titles").textContent;
const loading = () => screen.getByTestId("loading").textContent;

/* THE CENTRAL CONTRACT (#1007). Retention decides what is DRAWN FIRST, never whether to fetch.
 *
 * An earlier cut skipped the request entirely inside a 30 s freshness window, which made
 * correctness depend on every mutating surface announcing itself — and two review rounds found
 * surfaces that could not, ending with terminal-backed session creation, which issues no session
 * REST call for any transport rule to observe. Always revalidating removes that whole defect class
 * for a request that is a measured 0.1 ms cache hit inside the server's scan TTL (and a cold walk
 * after it, paid behind an already-rendered map). These tests exist to stop the window coming
 * back. */

test("a re-entry renders the retained rows immediately AND still revalidates (#1007)", async () => {
  mockSessions.mockResolvedValue(pageOf([sess("A"), sess("B")]));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("A,B"));
  expect(mockSessions).toHaveBeenCalledTimes(1);

  // Anything at all may have changed while away — a mission detach, a session created over the
  // websocket — so the return fetches unconditionally.
  mockSessions.mockResolvedValue(pageOf([sess("A"), sess("B"), sess("C")]));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);

  // Warm on the FIRST paint: the old rows are on screen and no spinner is shown...
  expect(titles()).toBe("A,B");
  expect(loading()).toBe("false");
  // ...while the refresh runs behind them and lands.
  await waitFor(() => expect(titles()).toBe("A,B,C"));
  expect(mockSessions).toHaveBeenCalledTimes(2);
});

test("a change made anywhere while away is reflected on return (#1007)", async () => {
  // Subsumes the mission-console and session-creation findings: neither surface announces
  // anything, and neither needs to.
  mockSessions.mockResolvedValue(pageOf([sess("Before")]));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("Before"));

  mockSessions.mockResolvedValue(pageOf([sess("Before"), sess("CreatedElsewhere")]));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);

  await waitFor(() => expect(titles()).toBe("Before,CreatedElsewhere"));
  expect(loading()).toBe("false"); // never through the blocking spinner
});

test("a failed refresh preserves the prior good result and surfaces no error (#1007)", async () => {
  mockSessions.mockResolvedValue(pageOf([sess("Good")]));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("Good"));

  mockSessions.mockRejectedValue(new Error("network"));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);

  await waitFor(() => expect(mockSessions).toHaveBeenCalledTimes(2));
  expect(titles()).toBe("Good");
  expect(screen.getByTestId("error").textContent).toBe("");
  expect(loading()).toBe("false");
});

test("a refresh failing MID-SEQUENCE keeps the whole prior result — never a truncated map (#1007)", async () => {
  // Page 1 succeeds and page 2 fails, so the accumulator holds a REAL but incomplete array.
  // Committing it would show fewer sessions than exist, and a "no second fetch" assertion would
  // happily pass.
  mockSessions
    .mockResolvedValueOnce(pageOf([sess("P1")], 200))
    .mockResolvedValueOnce(pageOf([sess("P2")]));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("P1,P2"));

  mockSessions.mockReset();
  mockSessions
    .mockResolvedValueOnce(pageOf([sess("Half")], 200))
    .mockRejectedValueOnce(new Error("scan failed"));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);

  await waitFor(() => expect(mockSessions).toHaveBeenCalledTimes(2));
  // "Half" never reaches the canvas, alone or merged.
  expect(titles()).toBe("P1,P2");
  expect(screen.getByTestId("error").textContent).toBe("");
});

test("a cold failure with nothing retained DOES surface an error (#1007)", async () => {
  mockSessions.mockRejectedValue(new Error("network"));
  render(<Shell open />);
  await waitFor(() =>
    expect(screen.getByTestId("error").textContent).toBe("Couldn’t load sessions."),
  );
  expect(titles()).toBe("");
});

test("refetch() re-runs the sequence for a mutation made while the map is MOUNTED (#1007)", async () => {
  // The map's own ⋯ actions have no re-entry to ride, so they still need an explicit re-run.
  mockSessions.mockResolvedValue(pageOf([sess("Before")]));
  render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("Before"));

  mockSessions.mockResolvedValue(pageOf([sess("After")]));
  await userEvent.click(screen.getByRole("button", { name: "refetch" }));
  await waitFor(() => expect(titles()).toBe("After"));
  expect(mockSessions).toHaveBeenCalledTimes(2);
});

test("the page cap rides along with the retained result (#1007)", async () => {
  mockSessions
    .mockResolvedValueOnce(pageOf([sess("P1")], 200))
    .mockResolvedValueOnce(pageOf([sess("P2")]));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(titles()).toBe("P1,P2"));
  expect(screen.getByTestId("partial").textContent).toBe("false");

  mockSessions.mockResolvedValue(pageOf([sess("P1"), sess("P2")]));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);
  expect(titles()).toBe("P1,P2"); // warm on first paint
});

/* SCOPE — the one thing revalidation cannot fix by being fast. Rows from a boundary no longer in
 * effect are WRONG, not old: narrowing paints sessions the operator just excluded, and widening
 * shows FEWER sessions than exist. Neither may appear even for the one round trip a refresh takes. */
describe("hard scope", () => {
  test("a scope-incompatible retained result is never rendered (#1007)", async () => {
    mockSessions.mockResolvedValue(pageOf([sess("Kept"), sess("Excluded")]));
    const { rerender } = render(<Shell open />);
    await waitFor(() => expect(titles()).toBe("Kept,Excluded"));

    // The operator excludes a folder; config carries the server-applied change.
    mockSessions.mockResolvedValue(pageOf([sess("Kept")]));
    rerender(<Shell open={false} exclusions={["/x"]} />);
    rerender(<Shell open exclusions={["/x"]} />);

    // Nothing is painted underneath the refresh — it blocks instead, because there is nothing
    // correct to show.
    expect(titles()).toBe("");
    expect(loading()).toBe("true");
    await waitFor(() => expect(titles()).toBe("Kept"));
  });

  test("WIDENING scope restores rows the cached response never carried (#1007)", async () => {
    // The direction that shows FEWER sessions than exist.
    mockSessions.mockResolvedValue(pageOf([sess("Kept")]));
    const { rerender } = render(<Shell open exclusions={["/x"]} />);
    await waitFor(() => expect(titles()).toBe("Kept"));

    mockSessions.mockResolvedValue(pageOf([sess("Kept"), sess("Restored")]));
    rerender(<Shell open={false} exclusions={[]} />);
    rerender(<Shell open exclusions={[]} />);

    expect(titles()).toBe(""); // the old-scope rows are not shown while it refreshes
    await waitFor(() => expect(titles()).toBe("Kept,Restored"));
  });

  test("a sequence whose scope moved beneath it is discarded (#1007)", async () => {
    let release: ((p: SessionsPage) => void) | undefined;
    mockSessions.mockReturnValueOnce(
      new Promise<SessionsPage>((res) => {
        release = res;
      }),
    );
    const { rerender } = render(<Shell open />);

    mockSessions.mockResolvedValue(pageOf([sess("NewScope")]));
    rerender(<Shell open exclusions={["/x"]} />);
    await waitFor(() => expect(titles()).toBe("NewScope"));

    // The old-scope sequence resolves LAST; its rows describe a boundary no longer in effect.
    release?.(pageOf([sess("OldScope")]));
    await waitFor(() => expect(titles()).toBe("NewScope"));
  });
});

/** The generation guard is asserted DIRECTLY on the store, because the route's `alive` flag
 *  short-circuits the same scenario first: a sequence whose effect has already been cleaned up
 *  returns before it ever reaches `commit`. That makes `alive` the thing under test in any
 *  end-to-end staging of this, and leaves the guard that actually protects SHELL-owned state
 *  unexercised. It is the last line of defence, so it is pinned where it can be provoked. */
function StoreHarness() {
  const { retained, scopeKey, begin, commit } = useOverviewSessionsStore();
  const slow = useRef(0);
  const fast = useRef(0);
  return (
    <>
      <div data-testid="retained">
        {(retained?.sessions ?? []).map((s) => s.title).join(",")}
      </div>
      <button
        onClick={() => {
          slow.current = begin();
        }}
      >
        begin-slow
      </button>
      <button
        onClick={() => {
          fast.current = begin();
        }}
      >
        begin-fast
      </button>
      <button onClick={() => commit(fast.current, scopeKey, [sess("New")], false)}>
        commit-fast
      </button>
      <button onClick={() => commit(slow.current, scopeKey, [sess("Old")], false)}>
        commit-slow
      </button>
    </>
  );
}

const retainedTitles = () => screen.getByTestId("retained").textContent;
const press = (name: string) =>
  userEvent.click(screen.getByRole("button", { name }));

test("a LATE generation is discarded whole — it never overwrites newer data (#1007)", async () => {
  render(
    <Scope>
      <StoreHarness />
    </Scope>,
  );
  await press("begin-slow");
  await press("begin-fast");

  await press("commit-fast");
  expect(retainedTitles()).toBe("New");

  // A resolves LAST, carrying older rows. Dropped entirely — not merged, and not applied as the
  // partial array it is.
  await press("commit-slow");
  expect(retainedTitles()).toBe("New");
});
