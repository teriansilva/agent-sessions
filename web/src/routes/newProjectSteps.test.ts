/** The New project wizard's step rules (#1187). */
import { describe, expect, test } from "vitest";
import type { ProjectEntity } from "../types/api";
import { COLOR_PRESETS } from "../lib/projectColors";
import {
  folderOwner,
  folderPath,
  initialDraft,
  isDirty,
  nameClash,
  preselectColor,
  reachable,
  stepIndex,
  stepValid,
  suggestFolderName,
  validFolderName,
  type ProjectDraft,
} from "./newProjectSteps";

const ent = (over: Partial<ProjectEntity>): ProjectEntity => ({
  id: "p-1",
  name: "Cayoo",
  color: "",
  folders: [],
  default_folder: "",
  archived: false,
  created_at: 0,
  session_count: 0,
  ...over,
});

const draft = (over: Partial<ProjectDraft> = {}): ProjectDraft => ({
  ...initialDraft(),
  parent: "/home/u",
  ...over,
});

describe("step validity", () => {
  test("NAME needs a non-blank name", () => {
    expect(stepValid("name", draft({ name: "  " }))).toBe(false);
    expect(stepValid("name", draft({ name: " Api " }))).toBe(true);
  });

  test("FOLDER (new) needs a parent and a one-segment name", () => {
    expect(stepValid("folder", draft({ folderName: "api" }))).toBe(true);
    expect(stepValid("folder", draft({ parent: "", folderName: "api" }))).toBe(false);
    for (const bad of ["", " ", ".", "..", "a/b", "a\\b", "x\u0001", "x".repeat(256)])
      expect(stepValid("folder", draft({ folderName: bad }))).toBe(false);
  });

  test("FOLDER (existing) needs a picked path", () => {
    expect(stepValid("folder", draft({ folderMode: "existing" }))).toBe(false);
    expect(
      stepValid("folder", draft({ folderMode: "existing", existingPath: "/home/u/x" })),
    ).toBe(true);
  });

  test("COLOUR is always valid; REVIEW needs NAME and FOLDER; DONE is never a Next target", () => {
    expect(stepValid("colour", draft())).toBe(true);
    expect(stepValid("review", draft({ name: "A" }))).toBe(false);
    expect(stepValid("review", draft({ name: "A", folderName: "a" }))).toBe(true);
    expect(stepValid("done", draft({ name: "A", folderName: "a" }))).toBe(false);
  });

  test("the rail reaches a step only past valid ones, and never DONE", () => {
    const d = draft({ name: "A" });
    expect(reachable(stepIndex("folder"), d)).toBe(true);
    expect(reachable(stepIndex("colour"), d)).toBe(false);
    const full = draft({ name: "A", folderName: "a" });
    expect(reachable(stepIndex("review"), full)).toBe(true);
    expect(reachable(stepIndex("done"), full)).toBe(false);
  });
});

describe("the folder", () => {
  test("new folder = parent + trimmed name; existing = the pick", () => {
    expect(folderPath(draft({ parent: "/home/u/", folderName: " api " }))).toBe("/home/u/api");
    expect(folderPath(draft({ folderMode: "existing", existingPath: "/home/u/x" }))).toBe(
      "/home/u/x",
    );
    expect(folderPath(draft({ folderName: "a/b" }))).toBe("");
  });

  test("validFolderName mirrors fsbrowse._valid_name", () => {
    expect(validFolderName("my-project.v2")).toBe(true);
    expect(validFolderName("..")).toBe(false);
  });

  test("a suggestion is derived from the project name", () => {
    expect(suggestFolderName("  Payments API v2! ")).toBe("payments-api-v2");
    expect(suggestFolderName("***")).toBe("");
  });

  test("an overlapping folder of ANY project (archived too) is flagged: equal, above or below", () => {
    const owners = [
      ent({ id: "a", name: "A", folders: ["/home/u/a"] }),
      ent({ id: "z", name: "Z", folders: ["/home/u/z"], archived: true }),
    ];
    expect(folderOwner("/home/u/a", owners)?.id).toBe("a");
    expect(folderOwner("/home/u/a/sub", owners)?.id).toBe("a");
    expect(folderOwner("/home/u", owners)?.id).toBe("a");
    expect(folderOwner("/home/u/z", owners)?.id).toBe("z");
    // A shared PREFIX is not nesting.
    expect(folderOwner("/home/u/ab", owners)).toBeNull();
    expect(folderOwner("", owners)).toBeNull();
  });
});

describe("warnings and defaults", () => {
  test("a name clash with an ACTIVE project warns (case-insensitive); archived ones do not", () => {
    const ps = [ent({ name: "Cayoo" }), ent({ id: "p-2", name: "Old", archived: true })];
    expect(nameClash(" cayoo ", ps)?.id).toBe("p-1");
    expect(nameClash("old", ps)).toBeNull();
    expect(nameClash("", ps)).toBeNull();
  });

  test("the preselected colour is the first preset no active project uses, else none", () => {
    expect(preselectColor([])).toBe(COLOR_PRESETS[0]);
    expect(
      preselectColor([ent({ color: COLOR_PRESETS[0].toUpperCase() }), ent({ color: COLOR_PRESETS[1] })]),
    ).toBe(COLOR_PRESETS[2]);
    // An archived project's colour is free again.
    expect(preselectColor([ent({ color: COLOR_PRESETS[0], archived: true })])).toBe(COLOR_PRESETS[0]);
    expect(preselectColor(COLOR_PRESETS.map((color) => ent({ color })))).toBe("");
  });

  test("dirty means something the operator entered — not what the wizard filled in", () => {
    // Home parent, a suggested folder name and a preselected colour are the wizard's own.
    expect(isDirty(draft({ color: COLOR_PRESETS[0], folderName: "x" }))).toBe(false);
    expect(isDirty(draft({ name: "A" }))).toBe(true);
    expect(isDirty(draft({ folderMode: "existing" }))).toBe(true);
    expect(isDirty(draft({ makeDefault: true }))).toBe(true);
    expect(isDirty(draft({ touched: { parent: false, folderName: false, color: true } }))).toBe(true);
  });
});
