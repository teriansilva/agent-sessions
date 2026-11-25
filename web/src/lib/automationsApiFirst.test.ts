/** #1728 Phase 2 — terminal automations are labelled from the roster's runtime (nothing stamped on
 *  the row), and "Switch to API client" targets the family's API client the server admits, with
 *  the server's reason when it admits none. Engine ids come from the fixture, never a literal. */
import { afterEach, expect, test } from "vitest";
import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineFamily, EngineInfo, Session } from "../types/api";
import type { Automation, AutomationAction } from "../types/automations";
import {
  HELD_WORDS,
  NOT_AVAILABLE_WORDS,
  agentPicker,
  apiSwitchFor,
  isTerminalTarget,
  sessionPicker,
  switchBody,
} from "./automations";

const ENGINES = fixture.engines as EngineInfo[];
const API = ENGINES.find((e) => e.runtime === "api" && e.api?.source)!;
const CONSOLE = ENGINES.find((e) => e.id === API.api!.source)!;
const LONE = ENGINES.find((e) => e.runtime === "pty" && !ENGINES.some((x) => x.api?.source === e.id))!;

const FAMILIES: EngineFamily[] = [
  {
    family: API.family!,
    clients: [CONSOLE.id, API.id],
    interactive_default: { engine: API.id, reason: "api_ready" },
    unattended_admitted: [],
    reasons: {},
  },
  {
    family: LONE.family!,
    clients: [LONE.id],
    interactive_default: { engine: LONE.id, reason: "api_absent" },
    unattended_admitted: [],
    reasons: {},
  },
];

const session = (engine: string, bypass = true): AutomationAction => ({
  kind: "start_session",
  engine,
  model: null,
  folder: "/w",
  bypass,
  message: { text: "triage" },
});

afterEach(() => setRoster(ENGINES));

test("any console target is a terminal target; an API target and a mission are not", () => {
  setRoster(ENGINES, [], FAMILIES);
  expect(isTerminalTarget(session(CONSOLE.id))).toBe(true);
  expect(isTerminalTarget(session(LONE.id))).toBe(true);
  expect(isTerminalTarget(session(API.id))).toBe(false);
  expect(
    isTerminalTarget({ kind: "start_mission", project_id: "p", instruction: { text: "x" }, autonomy: "propose" }),
  ).toBe(false);
  expect(isTerminalTarget(null)).toBe(false);
});

test("Switch targets the family's admitted API client; disabled with the server's reason otherwise", () => {
  setRoster(ENGINES, [], FAMILIES);
  expect(apiSwitchFor(session(CONSOLE.id), [{ id: API.id, ok: true, reason: "" }])).toEqual({
    engine: API.id,
    ok: true,
    reason: "",
  });
  expect(
    apiSwitchFor(session(CONSOLE.id), [{ id: API.id, ok: false, reason: "cannot be started with a first message" }]),
  ).toEqual({ engine: API.id, ok: false, reason: "cannot be started with a first message" });
  // A family with no API client: nothing to switch to, no button.
  expect(apiSwitchFor(session(LONE.id), [])).toBeNull();
  // Already on the API client: nothing to switch.
  expect(apiSwitchFor(session(API.id), [{ id: API.id, ok: true, reason: "" }])).toBeNull();
  // An older server (no families): no switch is offered on a guess.
  setRoster(ENGINES);
  expect(apiSwitchFor(session(CONSOLE.id), [{ id: API.id, ok: true, reason: "" }])).toBeNull();
});

test("the switch body never carries the terminal's permission grant or a model", () => {
  const a = { id: "a1", revision: 7, action: session(CONSOLE.id, true) } as unknown as Automation;
  expect(switchBody(a, API.id)).toEqual({
    revision: 7,
    action: { ...session(CONSOLE.id), engine: API.id, model: null, bypass: false },
  });
  const mission = {
    id: "a2",
    revision: 1,
    action: { kind: "start_mission", project_id: "p", instruction: { text: "x" }, autonomy: "propose" },
  } as unknown as Automation;
  expect(switchBody(mission, API.id)).toBeNull();
});

// ---- #2244: the What step's pickers are API first --------------------------------------------------

const APIS = ENGINES.filter((e) => e.runtime === "api");
const CHAT = ENGINES.find((e) => e.runtime === "chat")!;
const TERMINAL = ENGINES.find((e) => e.kind === "terminal")!;
/** A host where every API client can start except the last one (not installed). */
const HOST = ENGINES.map((e) =>
  e.runtime === "api" ? { ...e, present: e.id !== APIS[APIS.length - 1].id } : e,
);
const label = (id: string) => `L(${id})`;
const NOT_ADMITTED = "this agent's API client can't run unattended yet";

test("the agent picker offers admitted API agents only, and explains only installed, held API agents", () => {
  setRoster(HOST);
  const [ok, held, odd, absent] = [APIS[0], APIS[1], APIS[2], APIS[APIS.length - 1]];
  const engines = [
    { id: CONSOLE.id, ok: true, reason: "" },
    { id: ok.id, ok: true, reason: "" },
    { id: held.id, ok: false, reason: NOT_ADMITTED },
    { id: odd.id, ok: false, reason: "this agent could not be checked" },
    { id: absent.id, ok: false, reason: `${absent.id}: not installed` },
    { id: LONE.id, ok: false, reason: "missing: unattended_launch" },
    { id: TERMINAL.id, ok: false, reason: `${TERMINAL.id}: not an agent engine` },
    { id: CHAT.id, ok: false, reason: `${CHAT.id}: not an agent engine` },
  ];
  const pick = agentPicker(engines, ["", ""], label);
  expect(pick.offered).toEqual([ok.id]);
  expect(pick.kept).toEqual([]);
  expect(pick.why).toEqual([
    { id: held.id, line: `L(${held.id}): ${HELD_WORDS}` },
    { id: odd.id, line: `L(${odd.id}): ${NOT_AVAILABLE_WORDS}` },
  ]);
  for (const w of pick.why) expect(w.line).not.toMatch(/unattended_launch|not installed|not an agent engine/);
  // The fallback never echoes the server's own reason, whatever it says.
  for (const e of engines) if (e.reason) for (const w of pick.why) expect(w.line).not.toContain(e.reason);
});

test("a stored target the picker doesn't offer is kept once; an offered one isn't repeated", () => {
  setRoster(HOST);
  const engines = [{ id: APIS[0].id, ok: true, reason: "" }, { id: CONSOLE.id, ok: true, reason: "" }];
  expect(agentPicker(engines, [CONSOLE.id, CONSOLE.id], label).kept).toEqual([CONSOLE.id]);
  expect(agentPicker(engines, [APIS[0].id], label).kept).toEqual([]);
});

test("before a roster loads nothing is offered or explained: nothing is guessed", () => {
  setRoster([]);
  const pick = agentPicker([{ id: APIS[0].id, ok: true, reason: "" }], [], label);
  expect(pick).toEqual({ offered: [], kept: [], why: [] });
});

test("the session picker offers API sessions, plus the stored target whatever it runs in", () => {
  setRoster(HOST);
  const s = (engine: string, n: number) => ({ id: `${engine}:${n}`, engine }) as unknown as Session;
  const api = s(APIS[0].id, 1);
  const term = s(CONSOLE.id, 2);
  const chat = s(CHAT.id, 3);
  expect(sessionPicker([api, term, chat], [])).toEqual([api]);
  expect(sessionPicker([api, term, chat], [term.id])).toEqual([api, term]);
});
