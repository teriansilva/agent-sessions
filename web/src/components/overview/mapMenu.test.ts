import { expect, test } from "vitest";
import { restoreFocus, windowsForSession } from "./mapMenu";

test("an archive closes the window whose ACTION key names the session — including a converged placeholder (#968)", () => {
  const windows = [
    // Launched under a placeholder, converged since: the transport key never changes.
    { key: "opencode:new-1b2c", actionKey: "opencode:ses_real" },
    { key: "claude:abc", actionKey: "claude:abc" },
    { key: "claude:other", actionKey: "claude:other" },
  ];
  expect(windowsForSession(windows, "opencode:ses_real").map((w) => w.key)).toEqual([
    "opencode:new-1b2c",
  ]);
  expect(windowsForSession(windows, "claude:abc").map((w) => w.key)).toEqual(["claude:abc"]);
  // The frozen placeholder is not a session the server knows, so it never matches.
  expect(windowsForSession(windows, "opencode:new-1b2c")).toEqual([]);
});

test("focus returns to the opener while it exists, and to the map once it is gone", () => {
  const map = document.createElement("div");
  map.tabIndex = -1;
  const opener = document.createElement("button");
  document.body.append(map, opener);

  restoreFocus(opener, map);
  expect(document.activeElement).toBe(opener);

  opener.remove(); // the archived chip took its ⋯ with it
  restoreFocus(opener, map);
  expect(document.activeElement).toBe(map);

  map.remove();
});
