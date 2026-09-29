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

// #1224: the mission turn's stream. The composer's tests mock this whole function, so its own
// contract — the last `turn` line is the result, an `error` line throws, a stream that ends
// without its turn is a cut, never a TypeError — is pinned here, against a real NDJSON body.
function ndjson(lines: unknown[], cut = false): Response {
  const body =
    lines.map((l) => JSON.stringify(l)).join("\n") + (cut ? "" : "\n");
  return new Response(body, {
    status: 200,
    headers: { "content-type": "application/x-ndjson" },
  });
}

test("missionMessageStream resolves with the turn line and hands every line before it to onEvent", async () => {
  const turn = { turn_id: "t1", state: "done", answer: "ok" };
  const fetch = vi.fn<ApiFetch>(async () =>
    ndjson([
      { type: "progress", step: "classify" },
      { type: "answer", final: false, answer: "early" },
      { type: "turn", status: 200, turn },
    ]),
  );
  const seen: unknown[] = [];
  try {
    setApiFetch(fetch);
    const out = await api.missionMessageStream(
      "msn 1",
      { message: "hi", turnId: "t1" },
      (ev) => seen.push(ev),
    );
    expect(out).toEqual(turn);
    expect(seen).toEqual([
      { type: "progress", step: "classify" },
      { type: "answer", final: false, answer: "early" },
    ]);
    expect(fetch.mock.calls[0][0]).toBe("/api/missions/msn%201/message/stream");
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({
      message: "hi",
      turn_id: "t1",
    });
  } finally {
    setApiFetch(null);
  }
});

test("missionMessageStream: a stream that ends without its turn is a CUT, with its own words", async () => {
  try {
    setApiFetch(async () => ndjson([{ type: "progress", step: "classify" }]));
    await expect(
      api.missionMessageStream("m", { message: "hi", turnId: "t1" }, () => {}),
    ).rejects.toMatchObject({
      status: 502,
      message: "The connection dropped before the turn finished.",
    });
    // A half-written last line is the same cut, not a JSON parse error.
    setApiFetch(
      async () =>
        new Response('{"type": "progress", "step": "classify"}\n{"type": "tu', {
          status: 200,
        }),
    );
    await expect(
      api.missionMessageStream("m", { message: "hi", turnId: "t1" }, () => {}),
    ).rejects.toMatchObject({ status: 502 });
  } finally {
    setApiFetch(null);
  }
});

test("missionMessageStream: an error line and a refusal before the stream both throw their status", async () => {
  try {
    setApiFetch(async () =>
      ndjson([
        {
          type: "error",
          status: 502,
          detail: "the chat backend failed (ReviewError)",
        },
      ]),
    );
    await expect(
      api.missionMessageStream("m", { message: "hi", turnId: "t1" }, () => {}),
    ).rejects.toMatchObject({
      status: 502,
      message: "the chat backend failed (ReviewError)",
    });
    setApiFetch(async () =>
      Response.json(
        { detail: "a question is already running" },
        { status: 409 },
      ),
    );
    await expect(
      api.missionMessageStream("m", { message: "hi", turnId: "t1" }, () => {}),
    ).rejects.toMatchObject({
      status: 409,
      message: "a question is already running",
    });
  } finally {
    setApiFetch(null);
  }
});
