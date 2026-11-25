import { render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { OverviewSessionsProvider } from "../app/OverviewSessionsContext";
import { api, sessionsUrl } from "../lib/api";
import type { AppConfig, Session, SessionsPage } from "../types/api";
import { useOverviewSessions } from "./useOverviewSessions";

// #1007 Phase 3: the map pins ONE server scan across its paging sequence. Kept in its own file
// so the Phase 2 cancellation tests in `OverviewSessionsContext.test.tsx` stay untouched.

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, sessions: vi.fn() } };
});

const mockSessions = vi.mocked(api.sessions);

function sess(n: number): Session {
  return {
    id: `claude:s${n}`,
    engine: "claude",
    uuid: `s${n}`,
    short_uuid: `s${n}`,
    cwd: "/x",
    project: { kind: "folder", id: "/x", name: "x" },
    last_mtime: 0,
    first_user_message: "",
    title: `s${n}`,
    sticky: false,
    archived: false,
  };
}

function page(n: number, next: number | null, snapshot?: string): SessionsPage {
  return {
    sessions: [sess(n)],
    next_offset: next,
    total: 4,
    facets: { projects: [], engines: [] },
    ...(snapshot ? { snapshot } : {}),
  };
}

function MapRoute() {
  const { sessions } = useOverviewSessions();
  return <div data-testid="titles">{sessions.map((s) => s.title).join(",")}</div>;
}

function Shell({ open }: { open: boolean }) {
  const cfg = { project_roots: [], folder_exclusions: [] } as unknown as AppConfig;
  return (
    <ConfigCtx.Provider value={cfg}>
      <OverviewSessionsProvider>
        {open ? <MapRoute /> : <div data-testid="away" />}
      </OverviewSessionsProvider>
    </ConfigCtx.Provider>
  );
}

const sentSnapshots = () => mockSessions.mock.calls.map(([q]) => q?.snapshot);

test("a map sequence asks for a pin, then passes back the NEWEST token on every page (#1007)", async () => {
  mockSessions
    .mockResolvedValueOnce(page(1, 200, "t1"))
    .mockResolvedValueOnce(page(2, 400, "t1"))
    // The server replaced an expired pin: the sequence must follow the replacement.
    .mockResolvedValueOnce(page(3, 600, "t2"))
    // A response without a token (an older server) keeps the last one rather than dropping it.
    .mockResolvedValueOnce(page(4, null));
  const { rerender } = render(<Shell open />);
  await waitFor(() => expect(screen.getByTestId("titles").textContent).toBe("s1,s2,s3,s4"));

  expect(sentSnapshots()).toEqual(["new", "t1", "t1", "t2"]);
  expect(mockSessions.mock.calls.map(([q]) => q?.offset)).toEqual([0, 200, 400, 600]);

  // A re-entry is a NEW sequence, so it asks for a fresh pin instead of reusing an old token.
  mockSessions.mockResolvedValueOnce(page(9, null, "t3"));
  rerender(<Shell open={false} />);
  rerender(<Shell open />);
  await waitFor(() => expect(mockSessions).toHaveBeenCalledTimes(5));
  expect(sentSnapshots()[4]).toBe("new");
});

test("only a query that asks for a pin carries one: the sidebar's URL is unchanged (#1007)", () => {
  expect(sessionsUrl({ limit: 20, offset: 0 })).toBe("/api/sessions?limit=20&offset=0&archived=0");
  expect(sessionsUrl({ limit: 200, offset: 200, archived: false, snapshot: "t1" })).toBe(
    "/api/sessions?limit=200&offset=200&archived=0&snapshot=t1",
  );
});
