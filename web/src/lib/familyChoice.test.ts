import { beforeEach, expect, test } from "vitest";
import {
  FAMILY_CHOICE_KEY,
  parseFamilyChoices,
  readFamilyChoices,
  rememberFamilyChoice,
  rememberedFor,
} from "./familyChoice";

beforeEach(() => localStorage.removeItem(FAMILY_CHOICE_KEY));

test("remembers, overrides and forgets a family's explicit choice (#1728)", () => {
  expect(readFamilyChoices()).toEqual({});
  rememberFamilyChoice("fam-a", "fam-a-console");
  rememberFamilyChoice("fam-b", "fam-b-api");
  expect(readFamilyChoices()).toEqual({ "fam-a": "fam-a-console", "fam-b": "fam-b-api" });
  rememberFamilyChoice("fam-a", "fam-a-api");
  expect(readFamilyChoices()["fam-a"]).toBe("fam-a-api");
  rememberFamilyChoice("fam-a", null);
  expect(readFamilyChoices()).toEqual({ "fam-b": "fam-b-api" });
});

test("a malformed, hostile or oversized stored value is ignored as a whole", () => {
  for (const raw of [
    null,
    "",
    "not json",
    JSON.stringify({ v: 2, choices: {} }),
    JSON.stringify({ v: 1, choices: [] }),
    JSON.stringify({ v: 1, choices: { "Bad Family": "x-y" } }),
    JSON.stringify({ v: 1, choices: { fam: "../../etc" } }),
    JSON.stringify({ v: 1, choices: { fam: 3 } }),
    JSON.stringify({ v: 1, choices: Object.fromEntries(Array.from({ length: 40 }, (_, i) => [`f${i}x`, "ab"])) }),
    "x".repeat(5000),
  ]) {
    expect(parseFamilyChoices(raw)).toBeNull();
  }
  localStorage.setItem(FAMILY_CHOICE_KEY, "{broken");
  expect(readFamilyChoices()).toEqual({});
});

test("a bad family or id is never written", () => {
  rememberFamilyChoice("Not A Slug", "fam-console");
  rememberFamilyChoice("fam", "<script>");
  expect(localStorage.getItem(FAMILY_CHOICE_KEY)).toBeNull();
});

test("a family named like an Object.prototype member reads as unremembered, never a function", () => {
  const empty = readFamilyChoices();
  // The map itself inherits nothing: a plain `{}` would answer Object's own function here.
  expect((empty as Record<string, unknown>)["constructor"]).toBeUndefined();
  for (const name of ["constructor", "tostring", "valueof", "hasownproperty"]) {
    expect(rememberedFor(empty, name)).toBeNull();
  }
  expect(rememberedFor(empty, "constructor")).toBeNull();
  // …and it is an ordinary key once the operator really picked a client for it.
  rememberFamilyChoice("constructor", "constructor-api");
  const kept = readFamilyChoices();
  expect(rememberedFor(kept, "constructor")).toBe("constructor-api");
  expect(Object.getPrototypeOf(kept)).toBeNull();
  rememberFamilyChoice("constructor", null);
  expect(rememberedFor(readFamilyChoices(), "constructor")).toBeNull();
});
