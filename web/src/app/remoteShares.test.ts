import { expect, test } from "vitest";
import { isRow } from "./remoteShares";

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
