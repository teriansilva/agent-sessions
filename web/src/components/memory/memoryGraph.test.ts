import { describe, expect, test } from "vitest";
import type { MemoryNote } from "../../types/memory";
import { linkedFrom, memoryGraph, sessionId } from "./memoryGraph";

const note = (slug: string, over: Partial<MemoryNote> = {}): MemoryNote => ({
  slug,
  name: slug,
  description: "",
  type: "finding",
  body: "",
  size: 1,
  created: 0,
  updated: 0,
  session: { key: "claude:1", engine: "claude", kind: "terminal", resolved: true },
  links: [],
  ...over,
});

describe("memoryGraph", () => {
  test("one node per writer, one per note, a wrote edge each and only links to notes that exist", () => {
    const notes = [
      note("a", { links: ["b", "gone", "a"] }),
      note("b", { type: "decision" }),
      note("c", { session: { key: "", engine: "codex", kind: "terminal", resolved: false } }),
      note("d", { session: { key: "", engine: "codex", kind: "terminal", resolved: false } }),
    ];
    const { nodes, edges } = memoryGraph(notes);
    expect(nodes.filter(n => n.kind === "session").map(n => n.id)).toEqual([
      "s:claude:1",
      "s:unresolved:codex:terminal",
    ]);
    expect(nodes.find(n => n.id === "s:unresolved:codex:terminal")?.session?.notes).toBe(2);
    expect(edges.filter(e => e.kind === "wrote")).toHaveLength(4);
    expect(edges.filter(e => e.kind === "links").map(e => e.id)).toEqual(["l:a>b"]);
    // Decisions are the first note column; sessions sit left of every note.
    const b = nodes.find(n => n.id === "n:b")!;
    const a = nodes.find(n => n.id === "n:a")!;
    expect(b.x).toBeLessThan(a.x);
    expect(Math.min(...nodes.filter(n => n.kind === "note").map(n => n.x))).toBeGreaterThan(0);
  });

  test("unresolved writers group by engine and launch kind, never by guessing a session", () => {
    expect(sessionId({ key: "", engine: "codex", kind: "api", resolved: false })).toBe("s:unresolved:codex:api");
    expect(sessionId({ key: "codex:9", engine: "codex", kind: "api", resolved: true })).toBe("s:codex:9");
  });

  test("linkedFrom lists the notes that link to a slug, not the slug itself", () => {
    const notes = [note("a", { links: ["b"] }), note("b", { links: ["b"] }), note("c")];
    expect(linkedFrom(notes, "b").map(n => n.slug)).toEqual(["a"]);
  });
});
