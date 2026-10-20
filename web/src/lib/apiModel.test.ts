import { expect, test } from "vitest";
import { resolveApiModel } from "./apiModel";

// #1511: one resolution for the pane and the recap.
test("an agent-default session names the model and level its client reported", () => {
  expect(
    resolveApiModel({ model_configured: "gpt-6-codex", effort_configured: "high" }),
  ).toEqual({ model: "gpt-6-codex", effort: "high", agentDefault: true });
});

test("a request outranks a report, and an observation outranks both", () => {
  const facts = {
    model_requested: "opus",
    model_configured: "claude-opus-5-5",
    effort_requested: "max",
    effort_configured: "low",
  };
  expect(resolveApiModel(facts)).toEqual({ model: "opus", effort: "max", agentDefault: false });
  expect(resolveApiModel({ ...facts, model_effective: "claude-opus-5-5-x" }).model).toBe(
    "claude-opus-5-5-x",
  );
});

test("an unreported level is the model's default, never a guess (claude reports none)", () => {
  expect(resolveApiModel({ model_configured: "claude-opus-5-5" }).effort).toBe("default effort");
  // opencode's default variant is named "default": no level either.
  expect(resolveApiModel({ model_configured: "local/b", effort_configured: "default" }).effort).toBe(
    "default effort",
  );
});

test("nothing reported yet is unknown, not a model", () => {
  expect(resolveApiModel({}).model).toBeNull();
});
