import { describe, expect, it } from "vitest";
import FIXTURE from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import type { PluginCatalog, PluginGeneration } from "../../types/plugins";
import { buildRows, catalogOnly, engineStatus, rowMatches, versionsOf, type RosterRow } from "./agentStatus";

const ENGINES = FIXTURE.engines as unknown as EngineInfo[];
const byId = (id: string) => ENGINES.find((e) => e.id === id)!;
// The fixture's source → API pairs, found by the manifest field (no engine named here).
const API = ENGINES.filter((e) => e.runtime === "api" && e.api?.source && ENGINES.some((s) => s.id === e.api!.source));
const PAIR = API[0];
const SOURCE = byId(PAIR.api!.source);

function gen(id: string, digest = "r1"): PluginGeneration {
  return {
    id,
    review: { recipe_digest: digest, entry: { manifest: { identity: { id, label: id, publisher: "p", version: "1" } } } } as never,
    required_checks: [],
    verification: null,
  };
}
function catalog(over: Partial<PluginCatalog> = {}): PluginCatalog {
  return { feed: { state: "ready", error: null }, catalog: [], plugins: [], operations: [], roster_generation: 1, roster_revision: null, ...over };
}
const live = (e: EngineInfo, over: Partial<EngineInfo> = {}) => ({ ...e, present: true, status: "active", ...over });
const agentRow = (rows: RosterRow[], id: string) => rows.find((r) => r.id === id)!;

describe("engineStatus — the #1408 matrix", () => {
  const e = live(SOURCE);
  it("is Ready with nothing to do", () => {
    expect(engineStatus(e, catalog())).toMatchObject({ rank: 8, word: "Ready", tone: "up", action: null });
  });
  it("Retiring outranks everything an installer could say", () => {
    const data = catalog({ operations: [{ id: "op", plugin_id: e.id, kind: "install", state: "running", created_at: 1, updated_at: 1, generation_id: null, review: null, review_digest: null, error: null }] });
    expect(engineStatus({ ...e, status: "retiring", status_reason: "gone" }, data)).toMatchObject({ rank: 2, word: "Retiring", reason: "gone" });
  });
  it("a pending operation is Setting up, with View operation", () => {
    const data = catalog({ operations: [{ id: "op1", plugin_id: e.id, kind: "verify", state: "running", created_at: 1, updated_at: 1, generation_id: null, review: null, review_digest: null, error: null }] });
    expect(engineStatus(e, data)).toMatchObject({ rank: 3, word: "Setting up", action: { label: "View operation" } });
  });
  it("a disabled installation is Disabled", () => {
    const data = catalog({ plugins: [{ id: e.id, active: "g1", candidate: "g1", enabled: false, generations: [gen("g1")] }] });
    // …with the way back: setup's Enable step for that generation (#1416 review).
    expect(engineStatus(e, data)).toMatchObject({ rank: 4, word: "Disabled", tone: "idle", action: { label: "Enable" } });
    expect(engineStatus(e, data).action!.to).toContain("generation=g1");
  });
  it("a candidate that is not the active one Needs setup, with Continue setup", () => {
    const data = catalog({ plugins: [{ id: e.id, active: "g1", candidate: "g2", enabled: true, generations: [gen("g1"), gen("g2")] }] });
    expect(engineStatus(e, data)).toMatchObject({ rank: 5, word: "Needs setup", action: { label: "Continue setup" } });
  });
  it("a console agent without a binary is Not installed; an API client without its source is Unavailable", () => {
    expect(engineStatus(live(SOURCE, { present: false }), catalog())).toMatchObject({ rank: 6, word: "Not installed" });
    expect(
      engineStatus(live(PAIR, { present: false, api: { ...PAIR.api!, unavailable_reason: "source not signed in" } }), catalog()),
    ).toMatchObject({ rank: 6, word: "Unavailable", reason: "source not signed in" });
  });
  it("a newer catalog recipe is Update available, with a one-click Update (#1741)", () => {
    const data = catalog({
      plugins: [{ id: e.id, active: "g1", candidate: "g1", enabled: true, generations: [gen("g1", "old")] }],
      catalog: [{ manifest: { identity: { id: e.id, label: e.label, publisher: "p", version: "2" } }, digest: "new", installable: true }],
    });
    expect(engineStatus(e, data)).toMatchObject({ rank: 7, word: "Update available", action: { label: "Update" } });
    expect(engineStatus(e, data).action!.to).toMatch(/update=1/);
  });
  it("an active install whose candidate IS the active one is just Ready", () => {
    const data = catalog({ plugins: [{ id: e.id, active: "g1", candidate: "g1", enabled: true, generations: [gen("g1")] }] });
    expect(engineStatus(e, data).word).toBe("Ready");
  });
});

describe("buildRows — one row per CLI", () => {
  const engines = ENGINES.map((e) => live(e));
  it("an API client becomes a mode of its source's row, keyed on api.source", () => {
    const rows = buildRows(engines, [], catalog());
    const row = agentRow(rows, SOURCE.id);
    expect(row.kind === "agent" && row.modes.map((m) => m.engine.id)).toEqual([SOURCE.id, PAIR.id]);
    expect(rows.some((r) => r.id === PAIR.id)).toBe(false);
  });
  it("a row reads its WORST mode's word, never an aggregate", () => {
    const rows = buildRows(engines.map((e) => (e.id === PAIR.id ? { ...e, present: false } : e)), [], catalog());
    expect(agentRow(rows, SOURCE.id).status.word).toBe("Unavailable");
  });
  it("a grouped row whose API mode is retiring needs attention", () => {
    const rows = buildRows(engines.map((e) => (e.id === PAIR.id ? { ...e, status: "retiring" } : e)), [], catalog());
    const row = agentRow(rows, SOURCE.id);
    expect(row.status.word).toBe("Retiring");
    expect(rowMatches(row, "", "Needs attention")).toBe(true);
    expect(rowMatches(row, "", "Ready")).toBe(false);
  });
  it("an API client whose source left the roster keeps a row of its own", () => {
    const rows = buildRows(engines.filter((e) => e.id !== SOURCE.id), [], catalog());
    expect(rows.some((r) => r.id === PAIR.id)).toBe(true);
  });
  it("an invalid manifest is an Invalid row; a saved-only install is a row; catalog-only is not", () => {
    const data = catalog({
      plugins: [{ id: "saved-agent", active: "g1", candidate: "g1", enabled: false, generations: [gen("g1")] }],
      catalog: [{ manifest: { identity: { id: "new-agent", label: "New agent", publisher: "p", version: "1" } }, digest: "d", installable: true }],
    });
    const rows = buildRows(engines, [{ source: "plugins/x/acme/plugin.toml", error: "bad" }], data);
    expect(rows.find((r) => r.kind === "problem")).toMatchObject({ label: "acme", status: { word: "Invalid", rank: 1 } });
    expect(rows.find((r) => r.id === "saved-agent")).toMatchObject({ kind: "saved", status: { word: "Disabled" } });
    expect(rows.some((r) => r.id === "new-agent")).toBe(false);
    expect(catalogOnly(engines, data).map((c) => c.manifest.identity.id)).toEqual(["new-agent"]);
  });
  it("search matches a mode's id too, and the filters split ready from attention", () => {
    const rows = buildRows(engines, [], catalog());
    const row = agentRow(rows, SOURCE.id);
    expect(rowMatches(row, PAIR.id, "All")).toBe(true);
    expect(rowMatches(row, "zzz-nothing", "All")).toBe(false);
    expect(rowMatches(row, "", "Ready")).toBe(true);
    expect(rowMatches(row, "", "Needs attention")).toBe(false);
  });
});

describe("versionsOf — installed → available, and the rollback target (#1741)", () => {
  const verified = (id: string, version: string, digest = id): PluginGeneration => ({
    id,
    review: {
      digest: `d-${id}`,
      recipe_digest: digest,
      entry: { manifest: { identity: { id: "x", label: "X", publisher: "p", version }, install: { kind: "npm-prefix", version } } },
    } as never,
    required_checks: ["binary"],
    verification: { digest: `d-${id}`, checked_at: 1, results: [{ check: "binary", passed: true, detail: "" }] },
  });
  let seq = 0;
  const activated = (id: string, gen: string, at: number) => ({
    id, plugin_id: "x", kind: "activate" as const, state: "complete" as const, created_at: at, updated_at: at,
    generation_id: gen, review: null, review_digest: null, error: null, sequence: ++seq,
  });
  const entry = (version: string, digest: string) => ({
    manifest: { identity: { id: "x", label: "X", publisher: "p", version }, install: { kind: "npm-prefix", version } },
    digest,
    installable: true,
  });

  it("names the installed and the catalog's version when the recipe differs", () => {
    const data = catalog({
      plugins: [{ id: "x", active: "g1", candidate: "g1", enabled: true, generations: [verified("g1", "2.1.286", "r-old")] }],
      catalog: [entry("2.1.311", "r-new")],
      operations: [activated("a1", "g1", 1)],
    });
    expect(versionsOf("x", data)).toEqual({ installed: "2.1.286", available: "2.1.311", previous: null, undetermined: [] });
  });

  it("is current when the catalog pins the installed recipe", () => {
    const data = catalog({
      plugins: [{ id: "x", active: "g1", candidate: "g1", enabled: true, generations: [verified("g1", "1.19.4", "r")] }],
      catalog: [entry("1.19.4", "r")],
    });
    expect(versionsOf("x", data)).toMatchObject({ installed: "1.19.4", available: null });
  });

  it("offers the generation active before this one, if it still verifies", () => {
    const old = verified("g1", "2.1.286", "r-old");
    const data = catalog({
      plugins: [{ id: "x", active: "g2", candidate: "g2", enabled: true, generations: [old, verified("g2", "2.1.311", "r-new")] }],
      catalog: [entry("2.1.311", "r-new")],
      operations: [activated("a1", "g1", 1), activated("a2", "g2", 2)],
    });
    expect(versionsOf("x", data)).toEqual({ installed: "2.1.311", available: null, previous: { id: "g1", version: "2.1.286" }, undetermined: [] });
    // An unverified old generation is not a rollback target (activate would refuse it).
    old.verification = null;
    expect(versionsOf("x", data).previous).toBeNull();
  });

  it("takes the generation active immediately before, by commit sequence, even within one second", () => {
    // A → B → C, activated within one second (the manager's timestamps are whole seconds), and
    // listed in an order that says nothing (the state's keys are written sorted).
    const [a, b, c] = [activated("aA", "gA", 100), activated("aB", "gB", 100), activated("aC", "gC", 101)];
    const data = catalog({
      plugins: [{ id: "x", active: "gC", candidate: "gC", enabled: true,
        generations: [verified("gA", "1.0.0", "a"), verified("gB", "1.1.0", "b"), verified("gC", "1.2.0", "c")] }],
      catalog: [entry("1.2.0", "c")],
      operations: [b, c, a],
    });
    expect(versionsOf("x", data).previous).toEqual({ id: "gB", version: "1.1.0" });
    data.operations = [a, b, c];
    data.operations.forEach((o) => { o.updated_at = 100; o.created_at = 100; });
    expect(versionsOf("x", data).previous).toEqual({ id: "gB", version: "1.1.0" });
  });

  it("legacy history: sequenced activations decide; only a tie at the TOP of unsequenced history withholds", () => {
    const legacy = (id: string, gen: string, at: number) => ({ ...activated(id, gen, at), sequence: undefined });
    const gens = [verified("gA", "1.0.0", "a"), verified("gB", "1.1.0", "b"), verified("gC", "1.2.0", "c"), verified("gD", "1.3.0", "d")];
    // A then B in ONE second before the upgrade, C after it: which of A/B was last is unknowable.
    const tied = catalog({ plugins: [{ id: "x", active: "gC", candidate: "gC", enabled: true, generations: gens }],
      operations: [legacy("aA", "gA", 100), legacy("aB", "gB", 100), activated("aC", "gC", 101)] });
    expect(versionsOf("x", tied)).toMatchObject({
      previous: null,
      undetermined: [{ id: "gA", version: "1.0.0" }, { id: "gB", version: "1.1.0" }, { id: "gD", version: "1.3.0" }],
    });
    // The same old tie, then C and D activated after the upgrade (sequenced), D active: C is
    // unambiguously the previous one — the old tie can never matter again (Hermes 7319).
    const later = catalog({ plugins: [{ id: "x", active: "gD", candidate: "gD", enabled: true, generations: gens }],
      operations: [legacy("aA", "gA", 100), legacy("aB", "gB", 100), activated("aC", "gC", 101), activated("aD", "gD", 102)] });
    expect(versionsOf("x", later)).toMatchObject({ previous: { id: "gC", version: "1.2.0" }, undetermined: [] });
    // Unique legacy seconds: time orders them, so B is the previous one.
    const ordered = catalog({ plugins: [{ id: "x", active: "gC", candidate: "gC", enabled: true, generations: gens }],
      operations: [legacy("aA", "gA", 99), legacy("aB", "gB", 100), activated("aC", "gC", 101)] });
    expect(versionsOf("x", ordered)).toMatchObject({ previous: { id: "gB", version: "1.1.0" }, undetermined: [] });
    // A tie BELOW the newest unsequenced activation is irrelevant.
    const lowTie = catalog({ plugins: [{ id: "x", active: "gD", candidate: "gD", enabled: true, generations: gens }],
      operations: [legacy("aA", "gA", 100), legacy("aB", "gB", 100), legacy("aC", "gC", 101), legacy("aD", "gD", 102)] });
    expect(versionsOf("x", lowTie).previous).toEqual({ id: "gC", version: "1.2.0" });
  });

  it("offers no rollback for a disabled agent or one never activated before", () => {
    const g = [verified("g1", "1", "a"), verified("g2", "2", "b")];
    expect(versionsOf("x", catalog({ plugins: [{ id: "x", active: "g2", candidate: "g2", enabled: true, generations: g }], operations: [activated("a2", "g2", 2)] })).previous).toBeNull();
    expect(versionsOf("x", catalog({ plugins: [{ id: "x", active: "g2", candidate: "g2", enabled: false, generations: g }], operations: [activated("a1", "g1", 1), activated("a2", "g2", 2)] })).previous).toBeNull();
  });
});
