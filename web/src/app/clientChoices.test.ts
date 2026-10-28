/** The API-first default rule (#1739): a ready API client first, an unready one with its reason,
 *  and a console client only when no API client is ready. */
import { describe, expect, test } from "vitest";
import type { EngineInfo } from "../types/api";
import { apiClientOf, clientChoices } from "./engineRoster";

function engine(id: string, over: Partial<EngineInfo> = {}): EngineInfo {
  return {
    id,
    present: true,
    supports_new: true,
    supports_seed_start: false,
    seed_reason: null,
    bin: null,
    label: id.toUpperCase(),
    kind: "agent",
    runtime: "pty",
    display: { name: id, badge: id.slice(0, 2), accent: "slate", id_prefix: null, order: 1 },
    capabilities: {
      resume: true,
      new: true,
      archive: true,
      handoff_target: false,
      seed_start: false,
      orchestrator_input: false,
      raw_tty: false,
      owns_transcript: false,
    },
    session_id: { mint: "pinned" },
    models: [],
    usage: { source: "none" },
    terminal: { repaint: "none" },
    ...over,
  } as EngineInfo;
}

const apiOf = (id: string, source: string, reason: string | null, present = reason === null) =>
  engine(id, {
    runtime: "api",
    present,
    supports_new: present,
    api: { kind: "x", source, unavailable_reason: reason },
  });

describe("clientChoices — API first (#1739)", () => {
  test("a ready API client comes first and is preselected over a ready console client", () => {
    const engines = [engine("alpha"), apiOf("alpha-api", "alpha", null)];
    const c = clientChoices(engines, ["alpha", "alpha-api"]);
    expect(c.api).toEqual(["alpha-api"]);
    expect(c.terminal).toEqual(["alpha"]);
    expect(c.unready).toEqual([]);
    expect(c.preselect).toBe("alpha-api");
  });

  test("an API client that cannot start is listed with its reason, never dropped, never preselected", () => {
    const engines = [engine("alpha"), apiOf("alpha-api", "alpha", "alpha 2.0 or later is required")];
    const c = clientChoices(engines, ["alpha"]);
    expect(c.api).toEqual([]);
    expect(c.unready).toEqual([
      { id: "alpha-api", label: "ALPHA-API", reason: "alpha 2.0 or later is required" },
    ]);
    expect(c.preselect).toBe("alpha");
  });

  test("the server's unavailable list wins over the roster's reason, once per client", () => {
    const engines = [engine("alpha"), apiOf("alpha-api", "alpha", "roster reason")];
    const c = clientChoices(engines, ["alpha"], [
      { id: "alpha-api", label: "Alpha API", reason: "needs a sign-in" },
      { id: "gone-api", label: "Gone API", reason: "its source is not installed" },
    ]);
    expect(c.unready.map((u) => [u.id, u.reason])).toEqual([
      ["alpha-api", "needs a sign-in"],
      ["gone-api", "its source is not installed"],
    ]);
  });

  test("with no API client at all, the console client is the default", () => {
    const c = clientChoices([engine("alpha"), engine("beta")], ["alpha", "beta"]);
    expect(c.api).toEqual([]);
    expect(c.unready).toEqual([]);
    expect(c.preselect).toBe("alpha");
  });

  test("only what the server says can start is offered; chat clients are neither group", () => {
    const engines = [
      engine("alpha"),
      engine("beta"),
      apiOf("alpha-api", "alpha", null),
      engine("talk", { runtime: "chat" }),
    ];
    const c = clientChoices(engines, ["beta", "talk"]);
    expect(c.terminal).toEqual(["beta"]);
    expect(c.api).toEqual([]);
    expect(c.unready.map((u) => u.id)).toEqual(["alpha-api"]);
    expect(c.preselect).toBe("beta");
  });

  test("nothing startable → nothing preselected", () => {
    expect(clientChoices([engine("alpha")], []).preselect).toBeNull();
  });
});

describe("apiClientOf", () => {
  test("pairs a console agent with the API client whose manifest names it as its source", () => {
    const engines = [engine("alpha"), engine("beta"), apiOf("alpha-api", "alpha", null)];
    expect(apiClientOf(engines, "alpha")?.id).toBe("alpha-api");
    expect(apiClientOf(engines, "beta")).toBeUndefined();
  });
});
