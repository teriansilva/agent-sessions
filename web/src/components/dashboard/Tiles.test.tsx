import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { DashboardSessions, Session, SessionsPage } from "../../types/api";
import { RecentSessionsTile, RunningTile } from "./Tiles";
import type { Polled } from "./usePolled";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { ...actual.api, runningSessions: vi.fn() } };
});

function session(i: number): Session {
  return {
    id: `claude:aaaaaaaa-0000-4000-8000-${String(i).padStart(12, "0")}`,
    title: `running ${i}`,
    engine: "claude",
    project: { kind: "project", id: "p1", name: "Alpha" },
    working: false,
    last_mtime: 1_800_000_000,
  } as unknown as Session;
}

function page(
  rows: Session[],
  total: number,
  next: number | null,
): SessionsPage {
  return {
    sessions: rows,
    total,
    next_offset: next,
    facets: { projects: [], engines: [] },
  };
}

function dash(total: number): Polled<DashboardSessions> {
  return {
    status: "ok",
    refreshFailed: false,
    data: {
      live: {
        health: "ok",
        total,
        working: 0,
        by_engine: {},
        rows: [],
      },
      recent: { total: 0, rows: [] },
    },
  };
}

const wrap = (ui: React.ReactElement) =>
  render(<MemoryRouter>{ui}</MemoryRouter>);

beforeEach(() => vi.mocked(api.runningSessions).mockReset());

describe("RunningTile — the drill-down behind the count (#1123, Hermes 5240)", () => {
  test("'All N running' reads EVERY page, never the first page passed off as all", async () => {
    const first = Array.from({ length: 200 }, (_, i) => session(i));
    vi.mocked(api.runningSessions).mockImplementation(
      async (_w, _l, offset = 0) =>
        offset === 0 ? page(first, 201, 200) : page([session(200)], 201, null),
    );
    wrap(<RunningTile res={dash(201)} retry={() => {}} />);
    await userEvent.click(screen.getByTestId("running-show-all"));
    await waitFor(() =>
      expect(screen.getByTestId("running-foot")).toHaveTextContent(
        "all 201 running",
      ),
    );
    expect(
      within(screen.getByTestId("running-all")).getAllByTestId("running-row"),
    ).toHaveLength(201);
  });

  test("the expanded list FOLLOWS every live read, and a successful empty one is empty", async () => {
    vi.mocked(api.runningSessions).mockResolvedValueOnce(
      page([session(1), session(2)], 2, null),
    );
    const { rerender } = wrap(<RunningTile res={dash(2)} retry={() => {}} />);
    await userEvent.click(screen.getByTestId("running-show-all"));
    await waitFor(() =>
      expect(
        within(screen.getByTestId("running-all")).getAllByTestId("running-row"),
      ).toHaveLength(2),
    );
    // The next poll: one DIFFERENT session is running now.
    vi.mocked(api.runningSessions).mockResolvedValueOnce(
      page([session(9)], 1, null),
    );
    rerender(
      <MemoryRouter>
        <RunningTile res={dash(1)} retry={() => {}} />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText("running 9")).toBeInTheDocument(),
    );
    expect(screen.queryByText("running 1")).not.toBeInTheDocument();
    // …and then none: an empty EXPANSION, not the preview.
    vi.mocked(api.runningSessions).mockResolvedValueOnce(page([], 0, null));
    rerender(
      <MemoryRouter>
        <RunningTile res={dash(0)} retry={() => {}} />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("running-foot")).toHaveTextContent(
        "all 0 running",
      ),
    );
    expect(screen.queryByTestId("running-row")).not.toBeInTheDocument();
  });

  test("an older answer landing late never overwrites a newer one", async () => {
    let releaseOld!: (p: SessionsPage) => void;
    vi.mocked(api.runningSessions)
      .mockImplementationOnce(() => new Promise((r) => (releaseOld = r)))
      .mockResolvedValueOnce(page([session(7)], 1, null));
    const { rerender } = wrap(<RunningTile res={dash(3)} retry={() => {}} />);
    await userEvent.click(screen.getByTestId("running-show-all"));
    rerender(
      <MemoryRouter>
        <RunningTile res={dash(1)} retry={() => {}} />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText("running 7")).toBeInTheDocument(),
    );
    releaseOld(page([session(1), session(2), session(3)], 3, null));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("running 1")).not.toBeInTheDocument();
  });
});

test("RecentSessionsTile says so when a refresh failed, instead of passing the old list off as current", () => {
  const res: Polled<DashboardSessions> = {
    ...dash(0),
    refreshFailed: true,
  } as Polled<DashboardSessions>;
  wrap(<RecentSessionsTile res={res} retry={() => {}} />);
  expect(screen.getByTestId("tile-refresh-error")).toBeInTheDocument();
});
