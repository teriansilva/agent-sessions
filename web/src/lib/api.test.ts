import { expect, test, vi } from "vitest";
import { api, loginRedirectUrl } from "./api";
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
