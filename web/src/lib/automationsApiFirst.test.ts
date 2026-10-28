/** #1728 Phase 2 — terminal automations are labelled from the roster's runtime (nothing stamped on
 *  the row), and "Switch to API client" targets the family's API client the server admits, with
 *  the server's reason when it admits none. Engine ids come from the fixture, never a literal. */
import { afterEach, expect, test } from "vitest";
import { setRoster } from "../app/engineRoster";
import fixture from "../test/roster.fixture.json";
import type { EngineFamily, EngineInfo } from "../types/api";
import type { Automation, AutomationAction } from "../types/automations";
import { apiSwitchFor, isTerminalTarget, switchBody } from "./automations";

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
