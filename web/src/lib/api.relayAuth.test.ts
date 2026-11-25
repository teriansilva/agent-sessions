import { afterEach, expect, test, vi } from "vitest";

/** #1735: through the relay the box's own server pages (/login, /change-password) are not
 *  reachable, so a refused request ends the relay session with a reason instead of navigating. */

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

async function fresh() {
  vi.resetModules(); // `redirecting` and the relay store are module state
  sessionStorage.clear(); // the sent history persists in storage, not in the module
  localStorage.clear();
  const assign = vi.fn();
  vi.stubGlobal("location", { assign, pathname: "/connect/dashboard", search: "", origin: "http://h" });
  const api = await import("./api");
  const relay = await import("../homefree/relayStatus");
  return { api, relay, assign };
}

const refuse = (status: number, detail?: string) => async () =>
  new Response(detail ? JSON.stringify({ detail }) : null, {
    status,
    headers: { "content-type": "application/json" },
  });

test("a 401 through the relay ends the session once with its reason, and never navigates", async () => {
  const { api, relay, assign } = await fresh();
  const end = vi.fn();
  relay.armRelay(end);
  api.setApiFetch(refuse(401));
  await expect(api.api.sessions()).rejects.toThrow();
  await expect(api.api.sessions()).rejects.toThrow(); // a second refusal in the same burst
  expect(end).toHaveBeenCalledTimes(1);
  expect(end).toHaveBeenCalledWith(api.RELAY_SIGN_IN_NEEDED);
  expect(assign).not.toHaveBeenCalled();
});

test("a required password change through the relay ends the session with that reason", async () => {
  const { api, relay, assign } = await fresh();
  const end = vi.fn();
  relay.armRelay(end);
  api.setApiFetch(refuse(403, "password change required"));
  await expect(api.api.sessions()).rejects.toThrow();
  expect(end).toHaveBeenCalledWith(api.RELAY_PASSWORD_CHANGE_NEEDED);
  expect(assign).not.toHaveBeenCalled();
});

/** What the relay tunnel hands back for the box's `POST /logout` (#1735 review): the tunnel follows
 *  no redirect, so the 303 itself arrives, shaped as `tunnel.ts` builds it (status + headers + body). */
const tunnelled303 = async () =>
  new Response("", { status: 303, headers: { location: "/login" } });

test("signing out through the relay: the box's 303 confirms it — sent history cleared, relay ended, no navigation", async () => {
  const { api, relay, assign } = await fresh();
  const { appendSent, readSent } = await import("./sentHistory");
  const end = vi.fn();
  relay.publishRelay({ link: "linked", name: "box", status: "", remaining: "" }, end);
  appendSent({ text: "a secret prompt", attachments: [], session: "claude:s1" });
  api.setApiFetch(tunnelled303);
  await api.api.logout();
  expect(readSent()).toEqual([]);
  expect(end).toHaveBeenCalledWith(); // the operator's own Sign out: no reason
  expect(assign).not.toHaveBeenCalled();
});

test("a failed sign-out changes nothing: it throws, the history stays, the relay stays", async () => {
  const { api, relay, assign } = await fresh();
  const { appendSent, readSent } = await import("./sentHistory");
  const end = vi.fn();
  relay.publishRelay({ link: "linked", name: "box", status: "", remaining: "" }, end);
  appendSent({ text: "still here", attachments: [], session: "claude:s1" });
  api.setApiFetch(async () => new Response("boom", { status: 500 }));
  await expect(api.api.logout()).rejects.toThrow(/logout/);
  expect(readSent()).toHaveLength(1);
  expect(end).not.toHaveBeenCalled();
  expect(assign).not.toHaveBeenCalled();
});

test("on a direct connection nothing changes: a 401 goes to the login page", async () => {
  const { api, assign } = await fresh();
  api.setApiFetch(refuse(401));
  await expect(api.api.sessions()).rejects.toThrow();
  expect(assign).toHaveBeenCalledWith("/login?next=%2Fconnect%2Fdashboard");
});

test("a later attempt arms afresh: the latch from an ended session does not swallow its refusal", async () => {
  const { relay } = await fresh();
  const first = vi.fn();
  relay.armRelay(first);
  expect(relay.relayEnd("x")).toBe(true);
  relay.clearRelay();
  expect(relay.relayEnd("again")).toBe(true); // still the ended session's burst
  expect(first).toHaveBeenCalledTimes(1);
  const second = vi.fn();
  relay.armRelay(second);
  expect(relay.relayEnd("y")).toBe(true);
  expect(second).toHaveBeenCalledWith("y");
});
