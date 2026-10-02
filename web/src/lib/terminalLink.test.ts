import { afterEach, describe, expect, it, vi } from "vitest";
import { IN_APP_LINK_EVENT, openTerminalLink } from "./terminalLink";

afterEach(() => vi.unstubAllGlobals());

describe("openTerminalLink (#158, #1232)", () => {
  it("a BattleLab link on this origin opens in-app, not in a new tab", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(IN_APP_LINK_EVENT, on);
    openTerminalLink(`${window.location.origin}/s/claude/abc`);
    window.removeEventListener(IN_APP_LINK_EVENT, on);
    expect(open).not.toHaveBeenCalled();
    expect(seen).toEqual([
      { kind: "session", engine: "claude", id: "abc", path: "/s/claude/abc" },
    ]);
  });

  it("a ⌘/ctrl-click keeps it a new tab, with the referrer that stops a hand-off", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    openTerminalLink(`${window.location.origin}/s/claude/abc`, true);
    expect(open).toHaveBeenCalledWith(`${window.location.origin}/s/claude/abc`, "_blank", "noopener");
  });

  it("anything else opens in a new tab with no opener and no referrer", () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    openTerminalLink("https://example.com/s/claude/abc");
    expect(open).toHaveBeenCalledWith("https://example.com/s/claude/abc", "_blank", "noopener,noreferrer");
  });
});
