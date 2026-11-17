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

test("a terminal share is not drawn as a conversation row: this build has no screen pane (#2060)", () => {
  expect(isRow({ ...row, kind: "terminal" })).toBe(false);
  expect(isRow({ ...row, kind: "something-newer" })).toBe(false);
  expect(isRow({ ...row, kind: 7 })).toBe(false);
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
