import { describe, expect, test } from "vitest";
import { cleanLaunchDefaults, describeLaunchDefaults } from "./launchDefaults";

describe("project session defaults (#1508)", () => {
  test("clean keeps the dependency chain: a model only beside an engine, an effort only beside a model", () => {
    expect(cleanLaunchDefaults({ model: "m", effort: "high", bypass: false })).toEqual({ bypass: false });
    expect(cleanLaunchDefaults({ engine: "codex", effort: "high" })).toEqual({ engine: "codex" });
    expect(cleanLaunchDefaults({ engine: "codex", model: "m", effort: "high" })).toEqual({
      engine: "codex",
      model: "m",
      effort: "high",
    });
    expect(cleanLaunchDefaults({ engine: "", bypass: undefined })).toEqual({});
  });

  test("describe names what is inherited, so the line is never empty", () => {
    const global = { engine: "claude", bypass: true };
    expect(describeLaunchDefaults({}, global)).toMatch(/^global agent \(.+\) · skip prompts \(global\)$/);
    expect(describeLaunchDefaults({ engine: "codex", model: "gpt-5-codex", bypass: false }, global)).toMatch(
      /· gpt-5-codex · ask before acting$/,
    );
  });
});
