/** #2431: what the "newer version available" hint says — never "up to date" without a fresh answer,
 *  the registry's version verbatim, and Update from source's Review naming its actual target when
 *  the repository's head lags the registry. */
import { describe, expect, test } from "vitest";

import type { McpUpdateHint } from "../../types/mcp";
import { ago, availableChip, compareVersions, hintWords, lagWords, registryHost, throttledWords } from "./updateModel";

const NOW = 1_800_000_000;
const hint = (over: Partial<McpUpdateHint> = {}): McpUpdateHint => ({
  state: "unknown",
  installed: "3.3.0",
  latest: null,
  checked_at: null,
  error: null,
  tried_at: null,
  checking: false,
  ...over,
});

describe("ago", () => {
  test.each([
    [NOW - 5, "just now"],
    [NOW - 5 * 60, "5 min ago"],
    [NOW - 59 * 60, "59 min ago"],
    [NOW - 3 * 3600, "3 h ago"],
    [NOW - 26 * 3600, "1 d ago"],
    [NOW + 30, "just now"],
  ])("%s → %s", (at, words) => expect(ago(at, NOW)).toBe(words));
});

describe("availableChip", () => {
  test("the registry's version verbatim: a Go v is kept, never doubled; PyPI has none added", () => {
    expect(availableChip(hint({ state: "available", latest: "v3.4.0" }))).toBe("v3.4.0 available");
    expect(availableChip(hint({ state: "available", latest: "0.22.0" }))).toBe("0.22.0 available");
  });
  test("nothing unless a newer release is known", () => {
    expect(availableChip(null)).toBeNull();
    expect(availableChip(undefined)).toBeNull();
    expect(availableChip(hint({ state: "current", checked_at: NOW }))).toBeNull();
    expect(availableChip(hint())).toBeNull();
  });
});

describe("hintWords", () => {
  test("available: published, never a promise of what the action installs", () => {
    const w = hintWords(hint({ state: "available", latest: "3.4.0", checked_at: NOW - 3 * 3600 }), NOW);
    expect(w).toEqual({ answer: "3.4.0 published · checked 3 h ago", failed: null, checking: false, available: true });
    expect(w.answer).not.toMatch(/install|update to/i);
  });
  test("current: no newer release, with when", () => {
    expect(hintWords(hint({ state: "current", checked_at: NOW - 3 * 3600 }), NOW).answer).toBe(
      "No newer release · checked 3 h ago",
    );
  });
  test("unknown is never worded as up to date", () => {
    expect(hintWords(hint(), NOW).answer).toBe("Not checked yet");
    expect(hintWords(hint({ checked_at: NOW - 4 * 86_400 }), NOW).answer).toBe("Last checked over 3 days ago");
    for (const u of [hint(), hint({ checked_at: NOW - 4 * 86_400 }), hint({ error: "the npm answered 503", tried_at: NOW })])
      expect(`${hintWords(u, NOW).answer ?? ""} ${hintWords(u, NOW).failed ?? ""}`).not.toMatch(/up to date|no newer/i);
  });
  test("a failed attempt says so in the registry's words, and keeps the last answer", () => {
    const only = hintWords(hint({ error: "the npm answered 503", tried_at: NOW - 300 }), NOW);
    expect(only.failed).toBe("Couldn’t check (the npm answered 503) · 5 min ago");
    expect(only.answer).toBeNull();
    const kept = hintWords(
      hint({ state: "current", checked_at: NOW - 26 * 3600, error: "the PyPI could not be reached", tried_at: NOW - 300 }),
      NOW,
    );
    expect(kept.failed).toBe("Couldn’t check (the PyPI could not be reached) · 5 min ago");
    expect(kept.answer).toBe("No newer release · checked 1 d ago");
  });
  test("checking is carried through", () => {
    expect(hintWords(hint({ checking: true }), NOW).checking).toBe(true);
  });
});

test("throttledWords names the read it reuses", () => {
  expect(throttledWords(hint({ checked_at: NOW - 240 }), NOW)).toBe(
    "Checked 4 min ago. BattleLab reads a registry at most once every 10 minutes.",
  );
  expect(throttledWords(hint({ checked_at: NOW - 900, tried_at: NOW - 120 }), NOW)).toMatch(/^Checked 2 min ago\./);
  expect(throttledWords(null, NOW)).toMatch(/^Checked a moment ago\./);
});

test("registryHost per kind", () => {
  expect(registryHost("npm")).toBe("registry.npmjs.org");
  expect(registryHost("pypi")).toBe("pypi.org");
  expect(registryHost("go")).toBe("proxy.golang.org");
  expect(registryHost("remote")).toBe("");
});

describe("compareVersions", () => {
  test.each([
    ["3.3.1", "3.4.0", -1],
    ["v3.4.0", "3.4.0", 0],
    ["3.10.0", "3.9.9", 1],
    ["1.0", "1.0.0", 0],
    ["2.0.0-rc.1", "2.0.0", -1],
    ["v3.3.1-0.20261120100000-abcdef123456", "v3.4.0", -1],
    ["abc", "1.0.0", 0],
  ])("%s vs %s → %s", (a, b, want) => expect(compareVersions(a, b)).toBe(want));
});

describe("lagWords", () => {
  const avail = hint({ state: "available", latest: "3.4.0", checked_at: NOW });
  test("the head lags the registry: name both, install the head's", () => {
    expect(lagWords("3.3.1", avail)).toBe(
      "The repository’s head declares 3.3.1; the registry’s newest release is 3.4.0. Install 3.3.1, or wait until the repository catches up.",
    );
  });
  test("already up to date, but a newer release exists: the repository hasn't caught up", () => {
    expect(lagWords("3.3.0", avail, true)).toBe(
      "The repository’s head declares 3.3.0; the registry’s newest release is 3.4.0. There is nothing newer to install from the repository until it catches up.",
    );
  });
  test("the head is ahead of the hint: still names the actual target", () => {
    expect(lagWords("3.5.0", avail)).toMatch(/This Review installs 3\.5\.0\.$/);
  });
  test("nothing when they agree (a Go v aside) or there is no newer release", () => {
    expect(lagWords("3.4.0", avail)).toBeNull();
    expect(lagWords("v3.4.0", hint({ state: "available", latest: "v3.4.0" }))).toBeNull();
    expect(lagWords("3.3.1", hint({ state: "current", checked_at: NOW }))).toBeNull();
    expect(lagWords("3.3.1", null)).toBeNull();
  });
});
