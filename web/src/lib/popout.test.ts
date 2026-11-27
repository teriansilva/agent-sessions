import { afterEach, expect, test, vi } from "vitest";
import { POPOUT_FEATURES, popOutSession, popoutPathFromKey, popoutWindowName } from "./popout";

afterEach(() => vi.restoreAllMocks());

test("the pop-out path is /pop/<engine>/<id>, encoded; a malformed key has none", () => {
  expect(popoutPathFromKey("claude:aaaa-1")).toBe("/pop/claude/aaaa-1");
  expect(popoutPathFromKey("opencode:ses_x:y")).toBe("/pop/opencode/ses_x%3Ay");
  expect(popoutPathFromKey("nocolon")).toBeNull();
});

test("one window per session: the name is stable and safe", () => {
  expect(popoutWindowName("codex-api:5b0d2c1e-8f3a")).toBe("battlelab-pop-codex-api_5b0d2c1e-8f3a");
  expect(popoutWindowName("claude:a")).toBe(popoutWindowName("claude:a"));
  expect(popoutWindowName("claude:a")).not.toBe(popoutWindowName("claude:b"));
});

test("popOutSession opens the pop-out route in the session's named popup window", () => {
  const focus = vi.fn();
  const open = vi.spyOn(window, "open").mockReturnValue({ focus } as unknown as Window);
  expect(popOutSession("claude:aaaa-1")).toBe(true);
  expect(open).toHaveBeenCalledWith("/pop/claude/aaaa-1", popoutWindowName("claude:aaaa-1"), POPOUT_FEATURES);
  expect(focus).toHaveBeenCalled();
});

test("a blocked popup or a malformed key reports that nothing opened", () => {
  vi.spyOn(window, "open").mockReturnValue(null);
  expect(popOutSession("claude:aaaa-1")).toBe(false);
  expect(popOutSession("nocolon")).toBe(false);
});
