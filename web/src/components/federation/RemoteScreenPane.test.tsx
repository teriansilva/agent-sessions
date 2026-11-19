import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { TERMINAL_NOT_SHOWN } from "../../lib/federation";
import {
  SCREEN_NOT_A_FRAME,
  SCREEN_UNAVAILABLE,
  SCREEN_UNAVAILABLE_DETAIL,
} from "../../lib/federationScreen";

// The screen pane (#2060) under <StrictMode> (main.tsx), and with frames a hostile peer could
// send. The journey itself is the browser spec's (e2e/share-terminal.spec.ts).
const api = vi.hoisted(() => ({
  federationRemote: vi.fn(),
  federationRemoteFrame: vi.fn(),
}));
vi.mock("../../lib/api", async (orig) => ({
  ...(await orig<typeof import("../../lib/api")>()),
  api,
}));

const { ApiError } = await import("../../lib/api");
const { RemoteScreenPane } = await import("./RemoteScreenPane");
const { resetRemoteSharesForTest } = await import("../../app/remoteShares");

const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const GRANT = "g7Jx9aBcDeFgHiJkLmNoPq";
const FRAME = {
  fingerprint: "a".repeat(32),
  cols: 24,
  rows: 3,
  lines: ["$ make deploy", "", "TOKEN=████████"],
  settled: true,
};

/** The first read answers `first`; every later one is held until the pane gives it up. */
function answerOnce(first: () => Promise<unknown>) {
  let n = 0;
  api.federationRemoteFrame.mockImplementation(
    (_p: string, _g: string, _h: string | null, _w: number, signal?: AbortSignal) =>
      n++ === 0
        ? first()
        : new Promise((_resolve, reject) =>
            signal?.addEventListener("abort", () => reject(new Error("aborted"))),
          ),
  );
}

let stored: string[];
beforeEach(() => {
  resetRemoteSharesForTest();
  Object.values(api).forEach((f) => f.mockReset());
  api.federationRemote.mockResolvedValue({ remote: [] });
  localStorage.clear();
  sessionStorage.clear();
  stored = [];
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k: string) {
    stored.push(k);
  });
});
afterEach(() => {
  vi.restoreAllMocks();
});

function open(strict: boolean) {
  const tree = (
    <MemoryRouter>
      <RemoteScreenPane peerId={GPU} grantId={GRANT} />
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

// #2055 review (P2), the same class here: an `alive` ref that the StrictMode replay's cleanup
// sets false and nothing sets again would discard every answer, and the pane would say
// "Reading the screen…" for ever. The poll is fenced by a per-effect AbortController instead.
test("under StrictMode the screen pane's poll shows the frame", async () => {
  answerOnce(() => Promise.resolve(FRAME));
  open(true);
  const text = await screen.findByTestId("remote-screen-text");
  expect(text).toHaveTextContent("$ make deploy");
  expect(text).toHaveAttribute("data-cols", "24");
  expect(screen.queryByText("Reading the screen…")).toBeNull();
  expect(screen.getByTestId("remote-state")).toHaveTextContent("connected");
});

test("under StrictMode the owner's refusal is shown, with what is never sent", async () => {
  answerOnce(() =>
    Promise.reject(new ApiError(503, "unavailable", { detail: "unavailable", code: "unavailable" })),
  );
  open(true);
  const state = await screen.findByTestId("remote-screen-unavailable");
  expect(state).toHaveAttribute("data-reason", "screen");
  expect(state).toHaveTextContent(SCREEN_UNAVAILABLE);
  expect(screen.getByTestId("remote-screen-why").textContent).toBe(TERMINAL_NOT_SHOWN);
  expect(state).toHaveTextContent(SCREEN_UNAVAILABLE_DETAIL);
});

// #2055 review (P1), the same class here: `rows`, `cols` and the lines come from a peer. The
// pane must not size, pad or loop by an unchecked one, and a frame that fails the check is
// "not viewable", never thrown on (a throw here would take the route down).
const HOSTILE: [string, Record<string, unknown>][] = [
  ["cols 1e300", { cols: 1e300 }],
  ["rows 1e300", { rows: 1e300 }],
  ["cols NaN", { cols: NaN }],
  ["rows NaN", { rows: NaN }],
  ["cols negative", { cols: -24 }],
  ["rows negative", { rows: -3 }],
  ["rows huge", { rows: 10_000_000 }],
  ["cols huge", { cols: 10_000_000 }],
  ["cols Infinity", { cols: Infinity }],
  ["a line longer than cols", { lines: ["x".repeat(25)] }],
  ["a line far longer than cols", { lines: ["x".repeat(2_000_000)] }],
  ["a non-string line", { lines: [{ length: 1e9 }] }],
  ["a null line", { lines: [null] }],
  ["more lines than rows", { lines: ["a", "b", "c", "d"] }],
  ["lines not an array", { lines: { length: 1e9 } }],
];

test.each(HOSTILE)("a frame with %s is shown as not viewable", async (_name, over) => {
  const errors: unknown[] = [];
  const onError = (e: ErrorEvent) => errors.push(e.error);
  window.addEventListener("error", onError);
  try {
    answerOnce(() => Promise.resolve({ ...FRAME, ...over }));
    open(false);
    const state = await screen.findByTestId("remote-screen-unavailable");
    expect(state).toHaveAttribute("data-reason", "frame");
    expect(state).toHaveTextContent(SCREEN_UNAVAILABLE);
    expect(screen.getByTestId("remote-screen-why").textContent).toBe(SCREEN_NOT_A_FRAME);
    expect(screen.queryByTestId("remote-screen-text")).toBeNull();
    expect(errors).toEqual([]);
  } finally {
    window.removeEventListener("error", onError);
  }
});

test("a frame's text is text: markup, an escape-looking sequence and a link are not parsed", async () => {
  const hostile = [
    '<img src=x onerror="alert(1)"><b>bold</b>',
    "\\x1b[31m red [link](https://example.test/a)",
    "https://example.test/b",
  ];
  answerOnce(() => Promise.resolve({ ...FRAME, cols: 60, lines: hostile }));
  open(false);
  const text = await screen.findByTestId("remote-screen-text");
  const rows = [...text.children].map((r) => r.textContent);
  expect(rows).toEqual([...hostile]);
  expect(text.querySelector("img, b, a, script")).toBeNull();
});

test("nothing of the screen goes to browser storage", async () => {
  answerOnce(() => Promise.resolve(FRAME));
  open(true);
  await screen.findByTestId("remote-screen-text");
  // The next read (after the 1 s floor between reads) carries the fingerprint; still nothing kept.
  await waitFor(() => expect(api.federationRemoteFrame).toHaveBeenCalledTimes(2), { timeout: 5000 });
  expect(api.federationRemoteFrame.mock.calls[1][2]).toBe(FRAME.fingerprint);
  expect(stored).toEqual([]);
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});
