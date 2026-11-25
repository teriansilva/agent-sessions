import { beforeEach, expect, test, vi } from "vitest";

const api = vi.hoisted(() => ({ federationRemote: vi.fn() }));
vi.mock("../lib/api", async (orig) => ({ ...(await orig<typeof import("../lib/api")>()), api }));

const { isRow, proveLeft, refreshRemoteShares, remoteSharesNow, resetRemoteSharesForTest } = await import(
  "./remoteShares"
);

const row = {
  peer_id: "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl",
  grant_id: "g1",
  title: "Tune the render queue",
  peer_name: "gpu-box",
  level: "view",
  expires_at: 1_800_000_000,
};

test("an API share is listed, with or without a kind (an owner before kinds has only API shares)", () => {
  expect(isRow(row)).toBe(true);
  expect(isRow({ ...row, kind: "api" })).toBe(true);
});

test("a terminal share is listed: this build has its screen pane (#2060)", () => {
  expect(isRow({ ...row, kind: "terminal" })).toBe(true);
});

test("a kind this build has no pane for is skipped, never drawn as another kind", () => {
  for (const kind of ["something-newer", "Terminal", "API", "", 7, null, {}, ["terminal"]])
    expect(isRow({ ...row, kind }), JSON.stringify(kind)).toBe(false);
});

test("a terminal share is held to every other check", () => {
  const t = { ...row, kind: "terminal" };
  expect(isRow({ ...t, title: 3 })).toBe(false);
  expect(isRow({ ...t, peer_name: undefined })).toBe(false);
  for (const expires_at of [1e300, Infinity, NaN, -1, 0, "1800000000"])
    expect(isRow({ ...t, expires_at }), String(expires_at)).toBe(false);
});

test("a malformed entry is not listed", () => {
  expect(isRow(null)).toBe(false);
  expect(isRow({ ...row, title: 3 })).toBe(false);
  expect(isRow({ ...row, grant_id: undefined })).toBe(false);
});

test("an expiry no browser can make a date of is not a row (#2055 review)", () => {
  // `1e300` is finite and was accepted; formatting it threw and took the whole UI down.
  for (const expires_at of [1e300, 8.64e12 + 1, Infinity, NaN, -1, 0, "1800000000", null])
    expect(isRow({ ...row, expires_at }), String(expires_at)).toBe(false);
  expect(isRow({ ...row, expires_at: 8.64e12 })).toBe(true);
});

// ---- the order of reads, and a share this instance left (#2201) ---------------------------------

const listedRow = (grant_id: string, revision = 1) => ({ ...row, grant_id, revision, kind: "api" });
const ids = () => remoteSharesNow().rows.map((r) => r.grant_id);

/** A read whose answer the test delivers by hand. */
function held() {
  let answer!: (rows: unknown[]) => void;
  let fail!: () => void;
  api.federationRemote.mockImplementationOnce(
    (signal?: AbortSignal) =>
      new Promise((resolve, reject) => {
        answer = (rows) => resolve({ remote: rows });
        fail = () => reject(new Error("failed"));
        signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  );
  return { answer: (rows: unknown[]) => answer(rows), fail: () => fail() };
}

beforeEach(() => {
  resetRemoteSharesForTest();
  api.federationRemote.mockReset();
});

test("a read says whether the list was read; a failed read changes nothing and proves nothing", async () => {
  api.federationRemote.mockResolvedValueOnce({ remote: [listedRow("g1"), listedRow("g2")] });
  expect(await refreshRemoteShares()).toBe(true);
  expect(ids()).toEqual(["g1", "g2"]);
  api.federationRemote.mockRejectedValueOnce(new Error("offline"));
  expect(await refreshRemoteShares()).toBe(false);
  expect(ids()).toEqual(["g1", "g2"]);
  expect(remoteSharesNow().loaded).toBe(true);
});

test("the newest read wins: an older answer is dropped, and its caller gets the newer outcome", async () => {
  const older = held();
  const first = refreshRemoteShares();
  api.federationRemote.mockResolvedValueOnce({ remote: [listedRow("g2")] });
  const second = refreshRemoteShares();
  older.answer([listedRow("g1"), listedRow("g2")]); // late, and already replaced
  expect(await second).toBe(true);
  expect(await first).toBe(true); // "the list on screen was read after you asked"
  expect(ids()).toEqual(["g2"]);
});

test("a share this instance left goes at once, and the read cut before the leave cannot bring it back", async () => {
  api.federationRemote.mockResolvedValueOnce({ remote: [listedRow("g1"), listedRow("g2")] });
  await refreshRemoteShares();
  const before = held(); // a poll cut BEFORE the owner ended the grant
  const poll = refreshRemoteShares();
  api.federationRemote.mockResolvedValue({ remote: [listedRow("g2")] }); // the server's list after it
  const left = proveLeft(row.peer_id, "g1");
  expect(ids()).toEqual(["g2"]); // at once, before any read answers
  before.answer([listedRow("g1"), listedRow("g2")]); // late: dropped, it was replaced
  await Promise.all([poll, left]);
  expect(ids()).toEqual(["g2"]);
});

test("after a leave the list is the server's again: nothing is remembered that could hide a share", async () => {
  api.federationRemote.mockResolvedValue({ remote: [listedRow("g1"), listedRow("g2")] });
  await refreshRemoteShares();
  // the re-read after the leave fails: the row that was taken off stays off, nothing else moves
  api.federationRemote.mockRejectedValueOnce(new Error("offline"));
  expect(await proveLeft(row.peer_id, "g1")).toBe(false);
  expect(ids()).toEqual(["g2"]);
  // the same session shared again is a NEW grant, and is listed
  api.federationRemote.mockResolvedValue({ remote: [listedRow("g3"), listedRow("g2")] });
  await refreshRemoteShares();
  expect(ids()).toEqual(["g3", "g2"]);
  // no client-side mask: whatever the server lists is shown, the left grant's id included
  // (the server never lists it again; that is its rule, `tests/test_federation_leave.py`)
  api.federationRemote.mockResolvedValue({ remote: [listedRow("g1")] });
  await refreshRemoteShares();
  expect(ids()).toEqual(["g1"]);
});
