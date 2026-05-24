import { expect, test } from "vitest";
import { loginRedirectUrl } from "./api";

test("loginRedirectUrl encodes the current location as the next param", () => {
  expect(loginRedirectUrl({ pathname: "/s/claude/abc", search: "" })).toBe(
    "/login?next=%2Fs%2Fclaude%2Fabc",
  );
  expect(loginRedirectUrl({ pathname: "/s/claude/abc", search: "?x=1" })).toBe(
    "/login?next=%2Fs%2Fclaude%2Fabc%3Fx%3D1",
  );
  expect(loginRedirectUrl({ pathname: "/", search: "" })).toBe("/login?next=%2F");
});
