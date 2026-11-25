/** The editor's reading of the server's placeholder table (#983 P2).
 *
 *  Driven by `tests/fixtures/direction_placeholders.json`, which pytest pins to
 *  `mission_directions.placeholder_table()`: the chips offered here are the server's table, so a row
 *  added there changes these expectations through the fixture rather than through a copy. */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import type { DirectionPlaceholder } from "../../types/api";

import { factLabel, insertPlaceholder, placeholdersFor } from "./directionPlaceholders";

const TABLE = JSON.parse(
  readFileSync(resolve(process.cwd(), "../tests/fixtures/direction_placeholders.json"), "utf8"),
) as DirectionPlaceholder[];

const names = (probe: string) => placeholdersFor(TABLE, probe).map((p) => p.name);

describe("placeholdersFor", () => {
  test("offers exactly the facts each probe can fill, in the server's order", () => {
    expect(names("forge_checks")).toEqual(["pr", "checks", "repo", "branch"]);
    expect(names("forge_pr")).toEqual(["pr", "pr_state", "repo", "branch"]);
    expect(names("forge_review")).toEqual(["pr", "review", "repo", "branch"]);
    expect(names("git_local")).toEqual(["branch"]);
  });

  test("a probe that fills nothing, or no table at all, offers nothing", () => {
    expect(names("none")).toEqual([]);
    expect(names("http_status")).toEqual([]);
    expect(placeholdersFor(undefined, "forge_checks")).toEqual([]);
    expect(placeholdersFor(null, "forge_checks")).toEqual([]);
    expect(placeholdersFor(TABLE, "")).toEqual([]);
  });

  test("every probe the table names is offered that placeholder, and no other probe is", () => {
    const probes = new Set(TABLE.flatMap((p) => p.probes));
    for (const p of TABLE) {
      for (const probe of probes) {
        expect(names(probe).includes(p.name), `${p.name} on ${probe}`).toBe(
          p.probes.includes(probe),
        );
      }
    }
  });

  test("a malformed row is skipped rather than offered", () => {
    const bad = [
      { name: "pr", hint: "PR number", probes: ["forge_pr"] },
      { name: 3, hint: "x", probes: ["forge_pr"] },
      { name: "branch", hint: "branch" },
    ] as unknown as DirectionPlaceholder[];
    expect(placeholdersFor(bad, "forge_pr").map((p) => p.name)).toEqual(["pr"]);
  });
});

describe("insertPlaceholder", () => {
  test("types the token at the caret and puts the caret after it", () => {
    expect(insertPlaceholder("PR # is red", 4, 4, "pr")).toEqual({
      text: "PR #{pr} is red",
      caret: 8,
    });
  });

  test("replaces a selection", () => {
    expect(insertPlaceholder("checks are XX now", 11, 13, "checks")).toEqual({
      text: "checks are {checks} now",
      caret: 19,
    });
  });

  test("clamps a stale or inverted selection instead of splicing past the end", () => {
    expect(insertPlaceholder("abc", 99, 120, "branch")).toEqual({ text: "abc{branch}", caret: 11 });
    expect(insertPlaceholder("abc", 2, 1, "repo")).toEqual({ text: "ab{repo}c", caret: 8 });
    expect(insertPlaceholder("", Number.NaN, Number.NaN, "pr")).toEqual({ text: "{pr}", caret: 4 });
  });
});

test("factLabel reads each fact the way its chip does", () => {
  expect(factLabel("pr", 412)).toBe("PR #412");
  expect(factLabel("pr_state", "merged")).toBe("PR merged");
  expect(factLabel("checks", "failure")).toBe("checks failure");
  expect(factLabel("review", "APPROVED")).toBe("review APPROVED");
  expect(factLabel("branch", "fix/upload-retry")).toBe("fix/upload-retry");
  expect(factLabel("repo", "acme/upload-service")).toBe("acme/upload-service");
});
