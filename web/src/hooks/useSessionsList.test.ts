import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import type { Session, SessionsPage } from "../types/api";
import { useSessionsList } from "./useSessionsList";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { sessions: vi.fn() } };
});

const mockSessions = vi.mocked(api.sessions);

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

beforeEach(() => mockSessions.mockReset());

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
