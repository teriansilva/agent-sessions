import { afterEach, describe, expect, it, vi } from "vitest";

/** The corner icon's "on HQ's page?" at the moment of a tap (PR #2440): the shell's last render
 *  OR the browser's URL read as a route path, under either build's base. */
afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
  window.history.replaceState(null, "", "/");
});

async function underBase(base: string, url: string) {
  vi.stubEnv("BASE_URL", base);
  vi.resetModules();
  window.history.replaceState(null, "", url);
  return import("./askPanel");
}

describe("hqPageAtTap", () => {
  it("the local build: /hq is the page even before the shell re-renders", async () => {
    const { hqPageAtTap } = await underBase("/", "/hq");
    expect(hqPageAtTap(false)).toBe(true);
  });

  it("the connect build: /connect/hq is the page even before the shell re-renders", async () => {
    const { hqPageAtTap } = await underBase("/connect/", "/connect/hq/ask_x");
    expect(hqPageAtTap(false)).toBe(true);
  });

  it("anywhere else follows the shell's render", async () => {
    const local = await underBase("/", "/dashboard");
    expect(local.hqPageAtTap(false)).toBe(false);
    expect(local.hqPageAtTap(true)).toBe(true);
    const relay = await underBase("/connect/", "/connect/dashboard");
    expect(relay.hqPageAtTap(false)).toBe(false);
  });
});
