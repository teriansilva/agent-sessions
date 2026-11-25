import { describe, expect, it } from "vitest";
import { connectedUrl, isPublicConnectHost } from "./connectUrl";

const at = (pathname: string, search = "") => ({ pathname, search });

describe("isPublicConnectHost", () => {
  it("treats battlelabos.com as the public connect page, so the relay is prefilled and locked", () => {
    expect(isPublicConnectHost("battlelabos.com")).toBe(true);
  });

  it("keeps the older battlelab.superstatus.io address and its subdomains public", () => {
    expect(isPublicConnectHost("battlelab.superstatus.io")).toBe(true);
    expect(isPublicConnectHost("staging.battlelab.superstatus.io")).toBe(true);
  });

  it("matches exact names only: look-alikes and unrelated hosts are not public", () => {
    for (const host of [
      "battlelabos.com.example",
      "evilbattlelabos.com",
      "battlelab.superstatus.io.example",
      "evilbattlelab.superstatus.io",
      "localhost",
      "127.0.0.1",
      "box.home.lan",
    ]) {
      expect(isPublicConnectHost(host), host).toBe(false);
    }
  });
});

describe("connectedUrl (#1735)", () => {
  it("keeps a deep /connect/ route, so the app opens where the operator reloaded", () => {
    expect(connectedUrl(at("/connect/s/claude-api/abc"), true)).toBe("/connect/s/claude-api/abc");
    expect(connectedUrl(at("/connect/settings/agents"), true)).toBe("/connect/settings/agents");
  });

  it("keeps only allowlisted route-state query keys", () => {
    expect(connectedUrl(at("/connect/mission", "?m=m1&key=secret&code=x"), true)).toBe(
      "/connect/mission?m=m1",
    );
    expect(connectedUrl(at("/connect/", "?key=secret"), true)).toBe("/connect/");
  });

  it("normalises the entry document and falls back for paths outside the base", () => {
    expect(connectedUrl(at("/connect"), true)).toBe("/connect/");
    expect(connectedUrl(at("/connect/index.html"), true)).toBe("/connect/");
    expect(connectedUrl(at("/elsewhere"), true)).toBe("/connect/");
    expect(connectedUrl(at("/connect.html", "?x=1"), false)).toBe("/connect.html");
  });
});
