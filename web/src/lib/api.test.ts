import { expect, test, vi } from "vitest";
import { api, type ApiFetch, loginRedirectUrl, setApiFetch } from "./api";
import { appendSent, readSent } from "./sentHistory";

test("loginRedirectUrl encodes the current location as the next param", () => {
  expect(loginRedirectUrl({ pathname: "/s/claude/abc", search: "" })).toBe(
    "/login?next=%2Fs%2Fclaude%2Fabc",
  );
  expect(loginRedirectUrl({ pathname: "/s/claude/abc", search: "?x=1" })).toBe(
    "/login?next=%2Fs%2Fclaude%2Fabc%3Fx%3D1",
  );
  expect(loginRedirectUrl({ pathname: "/", search: "" })).toBe(
    "/login?next=%2F",
  );
});

// #619: sent prompt text must not outlive the session on a shared device. The cleanup has to live
// on the REAL sign-out path (api.logout), not only in the sentHistory helper.
test("logout clears the sent-message history", async () => {
  const csrf = document.createElement("meta");
  csrf.name = "csrf-token";
  csrf.content = "t";
  document.head.appendChild(csrf);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(null, { status: 204 }))),
  );
  const assign = vi.fn();
  vi.stubGlobal("location", { assign, pathname: "/", search: "" });

  appendSent({
    text: "a secret prompt",
    attachments: [],
    session: "claude:s1",
  });
  expect(readSent()).toHaveLength(1);

  await api.logout();

  expect(readSent()).toEqual([]);
  expect(assign).toHaveBeenCalledWith("/login");
  vi.unstubAllGlobals();
  csrf.remove();
});

// #1007 Phase 2: the map cancels its paging sequence through `api.sessions`' signal. The fetch
// binding is swappable at runtime (`setApiFetch` — the Home Free tunnel), so the signal must reach
// whichever implementation is bound WHEN THE REQUEST IS MADE, not one captured earlier. And the
// sidebar, which passes no signal, must send exactly what it always sent.
test("api.sessions hands its signal to the fetch bound at call time; without one the request is unchanged", async () => {
  const page = () =>
    Response.json({ sessions: [], next_offset: null, total: 0, facets: { projects: [], engines: [] } });
  const before = vi.fn<ApiFetch>(async () => page());
  const after = vi.fn<ApiFetch>(async () => page());
  const ctl = new AbortController();
  try {
    setApiFetch(before);
    await api.sessions({ limit: 200, offset: 0, archived: false }, { signal: ctl.signal });
    expect(before.mock.calls[0][1]?.signal).toBe(ctl.signal);

    // Swapped between two pages of one sequence: the next page must ride the NEW binding.
    setApiFetch(after);
    await api.sessions({ limit: 200, offset: 200, archived: false }, { signal: ctl.signal });
    expect(before).toHaveBeenCalledTimes(1);
    expect(after.mock.calls[0][1]?.signal).toBe(ctl.signal);

    // The sidebar's call: no signal key at all, so its request (and its 15 s poll) is untouched.
    await api.sessions({ limit: 20, offset: 0 });
    expect(after.mock.calls[1][1]).toEqual({ credentials: "same-origin" });
  } finally {
    setApiFetch(null);
  }
});
