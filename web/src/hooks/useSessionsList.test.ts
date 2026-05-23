import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { Session, SessionsPage } from "../types/api";
import { useSessionsList } from "./useSessionsList";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { sessions: vi.fn(), archive: vi.fn(), unarchive: vi.fn() } };
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
    sort_key: 0,
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
