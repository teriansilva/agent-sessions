import { describe, expect, test } from "vitest";

import { artifactQueryString } from "../../lib/api";
import type { EngineInfo } from "../../types/api";
import {
  absoluteLink,
  ago,
  breakAtHyphens,
  formatSize,
  hasToolChannel,
  isImageEntry,
  isPublicOff,
  kindLabel,
  PREVIEW_SANDBOX,
  publicLinksOff,
  restartChange,
  restartLine,
  revLabel,
} from "./artifactModel";

describe("the kind tag", () => {
  test("a site is SITE; a file is its entry's extension; anything else is FILE", () => {
    expect(kindLabel("site", "index.html")).toBe("Site");
    expect(kindLabel("site", null)).toBe("Site");
    expect(kindLabel("file", "p95-2k.png")).toBe("PNG");
    expect(kindLabel("file", "reports/plan.pdf")).toBe("PDF");
    expect(kindLabel("file", "Makefile")).toBe("File");
    expect(kindLabel("file", ".env")).toBe("File");
    expect(kindLabel("file", "x.reallylongext")).toBe("File");
    expect(kindLabel("file", undefined)).toBe("File");
  });

  test("only an image FILE gets a thumbnail", () => {
    expect(isImageEntry("file", "chart.PNG")).toBe(true);
    expect(isImageEntry("file", "a.svg")).toBe(true);
    expect(isImageEntry("file", "a.pdf")).toBe(false);
    expect(isImageEntry("site", "logo.png")).toBe(false);
    expect(isImageEntry("file", null)).toBe(false);
  });
});

test("sizes, revisions and ages read as the boards write them (lower-case r4)", () => {
  expect(formatSize(14 * 1024)).toBe("14 KB");
  expect(formatSize(2.4 * 1024 ** 2)).toBe("2.4 MB");
  expect(formatSize(5 * 1024 ** 3)).toBe("5 GB");
  expect(formatSize(200 * 1024 ** 2)).toBe("200 MB");
  expect(formatSize(512)).toBe("512 B");
  expect(formatSize(-1)).toBe("—");
  expect(revLabel(4)).toBe("r4");
  const now = 1_000_000;
  expect(ago(now - 30, now)).toBe("just now");
  expect(ago(now - 720, now)).toBe("12 min ago");
  expect(ago(now - 7200, now)).toBe("2 h ago");
  expect(ago(now - 90_000, now)).toBe("yesterday");
  expect(ago(now - 3 * 86_400, now)).toBe("3 days ago");
});

describe("the restart notice", () => {
  test("Off → on says the agent has no tool yet; Private ↔ Public says the new mode applies at restart", () => {
    expect(restartChange("off", "private")).toBe("on");
    expect(restartChange("off", "public")).toBe("on");
    expect(restartChange("private", "public")).toBe("mode");
    expect(restartChange("public", "private")).toBe("mode");
    expect(restartChange("public", "off")).toBe("off");
    expect(restartChange("private", "private")).toBeNull();
    expect(restartLine("on", "codex")).toBe(
      "The running codex does not have the tool yet; it gets it at restart.",
    );
    expect(restartLine("mode", "codex")).toBe(
      "The new mode applies when the codex restarts.",
    );
    // The "does not have the tool yet" wording is ONLY for Off → on.
    expect(restartLine("mode", "x")).not.toContain(
      "does not have the tool yet",
    );
    expect(restartLine("off", "x")).not.toContain("does not have the tool yet");
  });
});

describe("the tool channel is the manifest's `tools.mcp`, nothing else", () => {
  const base = { tools: { mcp: "claude-json" } } as unknown as EngineInfo;
  test("declared, absent, unknown", () => {
    expect(hasToolChannel(base)).toBe(true);
    expect(hasToolChannel({ ...base, tools: { mcp: null } })).toBe(false);
    expect(hasToolChannel({ ...base, tools: { mcp: "" } })).toBe(false);
    // An older server that does not send `tools`, or an engine the roster does not know: no claim.
    expect(hasToolChannel({ ...base, tools: undefined })).toBeUndefined();
    expect(hasToolChannel(undefined)).toBeUndefined();
  });
});

describe("public links", () => {
  test("off only without a login; an unread config claims nothing", () => {
    expect(publicLinksOff("none")).toBe(true);
    expect(publicLinksOff("single-user")).toBe(false);
    expect(publicLinksOff(undefined)).toBe(false);
  });
  test("the server's 409 public_off is recognised, other refusals are not", () => {
    expect(
      isPublicOff({
        status: 409,
        record: { detail: "public_off", reason: "x" },
      }),
    ).toBe(true);
    expect(isPublicOff({ status: 409, record: { detail: "other" } })).toBe(
      false,
    );
    expect(isPublicOff({ status: 404, record: { detail: "public_off" } })).toBe(
      false,
    );
    expect(isPublicOff(new Error("x"))).toBe(false);
    expect(isPublicOff(null)).toBe(false);
  });
});

test("the preview sandbox never grants allow-same-origin", () => {
  const tokens = PREVIEW_SANDBOX.split(/\s+/);
  expect(tokens).not.toContain("allow-same-origin");
  expect(tokens).toEqual(
    expect.arrayContaining([
      "allow-scripts",
      "allow-forms",
      "allow-popups",
      "allow-popups-to-escape-sandbox",
      "allow-downloads",
    ]),
  );
});

test("links: absolute as the server sent them, else this origin; ids break only at hyphens", () => {
  expect(
    absoluteLink(
      "https://box.example/artifacts/1234-5678-9123-4567/",
      "https://other",
    ),
  ).toBe("https://box.example/artifacts/1234-5678-9123-4567/");
  expect(
    absoluteLink("/artifacts/1234-5678-9123-4567/", "https://box.example/"),
  ).toBe("https://box.example/artifacts/1234-5678-9123-4567/");
  expect(breakAtHyphens("1234-5678")).toBe("1234-​5678");
});

test("the list query leaves empty filters out and trims the search", () => {
  expect(artifactQueryString({})).toBe("");
  expect(artifactQueryString({ q: "  ", project: "" })).toBe("");
  expect(
    artifactQueryString({
      visibility: "public",
      q: " rate limit ",
      session: "claude:abc",
    }),
  ).toBe("?session=claude%3Aabc&visibility=public&q=rate+limit");
});
