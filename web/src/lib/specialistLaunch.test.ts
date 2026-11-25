import { expect, test } from "vitest";
import type { Specialist, SpecialistList } from "../types/specialists";
import {
  bindingHolds,
  bindingNote,
  bindingOf,
  restoreBinding,
  specialistOptions,
} from "./specialistLaunch";

function spec(over: Partial<Specialist> = {}, current: Partial<Specialist["current"]> = {}): Specialist {
  return {
    id: "implementer",
    revision: 1,
    archived: false,
    grade: null,
    live: 0,
    skills_missing: [],
    skills_outdated: [],
    ...over,
    current: {
      version: 3,
      title: "Implementer",
      summary: "",
      skills: [{ id: "tdd", revision: "r1" }],
      engine: "claude",
      model: "claude-opus-5",
      effort: null,
      capacity: 2,
      created_at: 0,
      ...current,
    },
  };
}

function list(specialists: Specialist[], over: Partial<SpecialistList> = {}): SpecialistList {
  return { specialists, templates: [], max_instances: 6, live_total: 0, ...over };
}

test("every unarchived specialist is listed; the full and the unstartable say why", () => {
  const opts = specialistOptions(
    list([
      spec(),
      spec({ id: "full", live: 2 }),
      spec({ id: "elsewhere" }, { engine: "gemini" }),
      spec({ id: "gone", archived: true }),
    ]),
    ["claude", "codex"],
  );
  expect(opts.map((o) => [o.id, o.blocked])).toEqual([
    ["implementer", null],
    ["full", "at capacity"],
    ["elsewhere", "its agent can’t start here"],
  ]);
  expect(opts[0].text).toBe("Implementer · claude · 0/2 running");
  expect(opts[1].text).toBe("Implementer · claude · 2/2 running · at capacity");
});

test("a full pool blocks every specialist", () => {
  const opts = specialistOptions(list([spec()], { live_total: 6, max_instances: 6 }), ["claude"]);
  expect(opts[0].blocked).toBe("the pool is full");
});

test("the binding holds only while the form shows its agent, model and (API) effort", () => {
  const b = bindingOf(spec({}, { effort: "high" }));
  const form = { engine: "claude", model: "claude-opus-5", effort: "low", isApi: false };
  expect(bindingHolds(b, form)).toBe(true); // a terminal agent has no effort
  expect(bindingHolds(b, { ...form, engine: "codex" })).toBe(false);
  expect(bindingHolds(b, { ...form, model: "default" })).toBe(false);
  expect(bindingHolds(b, { ...form, isApi: true })).toBe(false);
  expect(bindingHolds(b, { ...form, isApi: true, effort: "high" })).toBe(true);
});

test("the note names what binds", () => {
  expect(bindingNote(bindingOf(spec()))).toBe(
    "Implementer v3: its skills (tdd) load into this session, read-only, with its notes for this project. Changing the agent or model below starts a plain session instead.",
  );
  expect(bindingNote(bindingOf(spec({}, { skills: [] })))).toContain(
    "its notes for this project load into this session.",
  );
});

test("a specialist on an agent whose launch can't carry it (a chat agent) can't be chosen", () => {
  const opts = specialistOptions(
    list([spec(), spec({ id: "chat" }, { engine: "apichat" })]),
    ["claude", "apichat"],
    ["claude"],
  );
  expect(opts.map((o) => [o.id, o.blocked])).toEqual([
    ["implementer", null],
    ["chat", "a chat agent can’t run a specialist"],
  ]);
});

test("a choice carried through the detour is honoured only as it was", () => {
  const opts = specialistOptions(list([spec(), spec({ id: "full", live: 2 })]), ["claude"]);
  expect(restoreBinding(opts, { id: "implementer", version: 3 })?.id).toBe("implementer");
  expect(restoreBinding(opts, { id: "implementer", version: 2 })).toBeNull(); // a newer version
  expect(restoreBinding(opts, { id: "full", version: 3 })).toBeNull(); // can't start now
  expect(restoreBinding(opts, { id: "gone", version: 3 })).toBeNull();
});

test("a reply that isn't a pool offers nothing, never throws", () => {
  expect(specialistOptions({} as SpecialistList, ["claude"])).toEqual([]);
  expect(specialistOptions(null, ["claude"])).toEqual([]);
});
