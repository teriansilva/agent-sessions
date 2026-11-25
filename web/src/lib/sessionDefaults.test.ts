import { describe, expect, it } from "vitest";
import type { StructuredModelList } from "../types/api";
import { levelsOf, resolveSessionDefault } from "./sessionDefaults";

const LIST: StructuredModelList = {
  status: "ok",
  reason: null,
  models: [
    { id: "a/coder", label: "Coder", description: null, efforts: ["low", "high"], is_default: false },
    { id: "b/plain", label: "Plain", description: null, efforts: [], is_default: true },
  ],
};

describe("resolveSessionDefault (#1407)", () => {
  it("preselects a listed model and one of its levels", () => {
    expect(resolveSessionDefault({ model: "a/coder", effort: "high" }, LIST)).toMatchObject({
      model: "a/coder",
      effort: "high",
      modelStale: null,
      effortStale: null,
    });
  });

  it("nothing stored means the client's own default", () => {
    expect(resolveSessionDefault(undefined, LIST)).toMatchObject({ model: null, effort: null });
  });

  it("a model the client dropped is stale and NOT preselected", () => {
    expect(resolveSessionDefault({ model: "gone/x", effort: "high" }, LIST)).toMatchObject({
      model: null,
      effort: null,
      modelStale: "gone/x",
    });
  });

  it("a dropped level keeps the model and falls back to the model's own level", () => {
    expect(resolveSessionDefault({ model: "a/coder", effort: "max" }, LIST)).toMatchObject({
      model: "a/coder",
      effort: null,
      effortStale: "max",
    });
  });

  it("an unreadable list cannot vouch for a stored model", () => {
    const down: StructuredModelList = { status: "unavailable", models: [], reason: "offline" };
    expect(resolveSessionDefault({ model: "a/coder", effort: null }, down)).toMatchObject({
      model: null,
      unverified: true,
    });
  });

  it("while the list loads the stored values show as pending", () => {
    expect(resolveSessionDefault({ model: "a/coder", effort: "low" }, null)).toMatchObject({
      model: "a/coder",
      effort: "low",
      pending: true,
    });
  });
});

describe("levelsOf", () => {
  it("lists a model's own levels, none for default, a level-less or unknown model", () => {
    expect(levelsOf(LIST, "a/coder")).toEqual(["low", "high"]);
    expect(levelsOf(LIST, "b/plain")).toEqual([]);
    expect(levelsOf(LIST, "default")).toEqual([]);
    expect(levelsOf(LIST, "nope")).toEqual([]);
    expect(levelsOf(null, "a/coder")).toEqual([]);
  });
});
