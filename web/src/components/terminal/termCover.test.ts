import { expect, test } from "vitest";
import { termCover } from "./termCover";

// #1502: what covers a terminal's area, from its socket status and the row lookup for its key.
// Only a PENDING lookup may mask a 4404, and only a settled MISSING one is a not-found verdict.

const notFound = { kind: "rejected", reason: "session not found or ended", code: 4404 } as const;

test("4415 (not a terminal session) re-opens, whatever the lookup says", () => {
  for (const phase of ["pending", "found", "missing", "failed", null] as const)
    expect(termCover({ kind: "wrong-runtime" }, phase)).toBe("reopening");
});

test("a 4404 is 'Opening…' only while the lookup is pending", () => {
  expect(termCover(notFound, "pending")).toBe("opening");
});

test("a 4404 after a settled 404 is the real not-found result", () => {
  expect(termCover(notFound, "missing")).toBe("missing");
});

test("a 4404 with a failed lookup or an existing row keeps the chip — never masked, never 'missing'", () => {
  expect(termCover(notFound, "failed")).toBeNull();
  expect(termCover(notFound, "found")).toBeNull();
  expect(termCover(notFound, null)).toBeNull();
});

test("other refusals and live states are never covered", () => {
  const model = { kind: "rejected", reason: "x", code: 4422 } as const;
  expect(termCover(model, "pending")).toBeNull();
  expect(termCover({ kind: "connecting" }, "pending")).toBeNull();
  expect(termCover({ kind: "connected" }, "missing")).toBeNull();
});

test("a 4404 whose lookup resolved to an owner (an alias about to navigate) still reads 'Opening…'", () => {
  expect(termCover(notFound, "found", true)).toBe("opening");
  expect(termCover(notFound, "found", false)).toBeNull();
});

test("4412 (a stale automatic default, #1728) hands the launch back to the form, whatever the lookup", () => {
  const stale = { kind: "rejected", reason: "the default changed", code: 4412 } as const;
  for (const phase of ["pending", "found", "missing", "failed", null] as const)
    expect(termCover(stale, phase)).toBe("stale-default");
});
