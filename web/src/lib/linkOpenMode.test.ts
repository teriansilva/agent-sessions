import { afterEach, describe, expect, it } from "vitest";
import { LINK_OPEN_MODE_KEY, readLinkOpenMode, writeLinkOpenMode } from "./linkOpenMode";

afterEach(() => localStorage.clear());

describe("link open mode (#1232)", () => {
  it("defaults to ask", () => {
    expect(readLinkOpenMode()).toBe("ask");
  });

  it("round-trips a remembered choice", () => {
    writeLinkOpenMode("map");
    expect(readLinkOpenMode()).toBe("map");
    writeLinkOpenMode("fullscreen");
    expect(readLinkOpenMode()).toBe("fullscreen");
  });

  it("ask is the reset: it clears the stored value", () => {
    writeLinkOpenMode("map");
    writeLinkOpenMode("ask");
    expect(localStorage.getItem(LINK_OPEN_MODE_KEY)).toBeNull();
    expect(readLinkOpenMode()).toBe("ask");
  });

  it("an unknown stored value reads as ask", () => {
    localStorage.setItem(LINK_OPEN_MODE_KEY, "sideways");
    expect(readLinkOpenMode()).toBe("ask");
  });
});
