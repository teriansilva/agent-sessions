/** The wizard's return map and New-session handoff (#1187). The map is the open-redirect guard:
 *  only its own keys resolve, to route constants, and nothing is ever read as a URL. */
import { describe, expect, test } from "vitest";
import {
  cancelState,
  finishState,
  NEW_PROJECT_RETURN,
  readDraft,
  readLandingRestore,
  readWizardEntry,
  returnTarget,
  type NewSessionDraft,
} from "./newProject";
import { DASHBOARD_PATH, MAP_PATH, SESSIONS_PATH } from "./routes";
import { settingsPath } from "../routes/settingsTabs";

const DRAFT: NewSessionDraft = {
  engineChoice: "codex",
  bypassChoice: false,
  returnTo: MAP_PATH,
  projectChoice: "",
  cwdOverride: "/home/u/x",
};

describe("returnTarget — a closed map", () => {
  test("the three entry points resolve to their route constants", () => {
    expect(returnTarget("new-session")).toBe(SESSIONS_PATH);
    expect(returnTarget("settings-projects")).toBe(settingsPath("projects"));
    expect(returnTarget("dashboard")).toBe(DASHBOARD_PATH);
    expect(Object.keys(NEW_PROJECT_RETURN)).toHaveLength(3);
  });

  test.each([
    ["https://evil.example"],
    ["//evil.example"],
    ["/settings"],
    ["constructor"],
    ["__proto__"],
    ["toString"],
    [""],
    [null],
    [undefined],
    [42],
    [{ from: "dashboard" }],
  ])("%j has no target", (v) => {
    expect(returnTarget(v)).toBeNull();
  });
});

describe("readWizardEntry", () => {
  test("keeps a known key and, for New session only, its draft", () => {
    expect(readWizardEntry({ from: "new-session", draft: DRAFT })).toEqual({
      from: "new-session",
      draft: DRAFT,
    });
    expect(readWizardEntry({ from: "dashboard", draft: DRAFT })).toEqual({
      from: "dashboard",
      draft: null,
    });
  });

  test("an unknown or missing key is null — DONE then offers only its own actions", () => {
    expect(readWizardEntry({ from: "/evil" })).toEqual({ from: null, draft: null });
    expect(readWizardEntry(null)).toEqual({ from: null, draft: null });
    expect(readWizardEntry("new-session")).toEqual({ from: null, draft: null });
  });
});

describe("readDraft", () => {
  test("keeps projectChoice's three states", () => {
    expect(readDraft({ ...DRAFT, projectChoice: null })?.projectChoice).toBeNull();
    expect(readDraft({ ...DRAFT, projectChoice: "" })?.projectChoice).toBe("");
    expect(readDraft({ ...DRAFT, projectChoice: "p-1" })?.projectChoice).toBe("p-1");
  });

  test("coerces malformed fields instead of trusting them", () => {
    expect(
      readDraft({
        engineChoice: 7,
        bypassChoice: "yes",
        returnTo: "https://evil.example",
        projectChoice: 3,
        cwdOverride: {},
      }),
    ).toEqual({
      engineChoice: "",
      bypassChoice: null,
      returnTo: null,
      projectChoice: null,
      cwdOverride: null,
    });
    expect(readDraft("x")).toBeNull();
    expect(readDraft(null)).toBeNull();
  });
});

describe("the New session handoff", () => {
  test("cancel restores the draft exactly and never carries a project id", () => {
    const s = cancelState(DRAFT);
    expect(s).toEqual({ returnTo: MAP_PATH, restoreDraft: DRAFT });
    expect(s).not.toHaveProperty("selectProjectId");
    expect(cancelState(null)).toEqual({});
  });

  test("finish selects the new project and keeps agent/bypass/map return", () => {
    expect(finishState(DRAFT, "p-new")).toEqual({
      returnTo: MAP_PATH,
      restoreDraft: { ...DRAFT, projectChoice: null, cwdOverride: null },
      selectProjectId: "p-new",
    });
    expect(finishState(null, "p-new")).toEqual({ selectProjectId: "p-new" });
  });

  test("New session reads both back, round-trip", () => {
    expect(readLandingRestore(finishState(DRAFT, "p-new"))).toEqual({
      draft: { ...DRAFT, projectChoice: null, cwdOverride: null },
      selectProjectId: "p-new",
    });
    expect(readLandingRestore(cancelState(DRAFT))).toEqual({
      draft: DRAFT,
      selectProjectId: null,
    });
    expect(readLandingRestore({ selectProjectId: "" })).toEqual({
      draft: null,
      selectProjectId: null,
    });
  });
});
