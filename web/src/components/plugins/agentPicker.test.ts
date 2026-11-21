import { describe, expect, it } from "vitest";
import type { RosterSnapshot } from "../../app/engineRoster";
import type { EngineInfo } from "../../types/api";
import type { PluginCatalog, PluginManifest } from "../../types/plugins";
import { agentPicker } from "./agentPicker";

const agent = (id: string): PluginManifest => ({ identity: { id, label: `Label ${id}`, version: "1.0.0", publisher: "p" } });
const client = (id: string, source: string): PluginManifest =>
  ({ identity: { id, label: `Label ${id}`, version: "1", publisher: "p" }, runtime: { kind: "api" }, api: { kind: "acp", source } });

function catalog(plugins: PluginCatalog["plugins"] = []): PluginCatalog {
  return {
    feed: { state: "ready", error: null },
    catalog: [
      { manifest: agent("alpha"), digest: "a", installable: true, included: false, reason: null },
      { manifest: agent("beta"), digest: "b", installable: false, included: false, reason: "not listed" },
      { manifest: agent("gamma"), digest: "c", installable: false, included: false, reason: "not listed" },
      { manifest: client("alpha-api", "alpha"), digest: "d", installable: false, included: true, reason: "included" },
      { manifest: client("beta-api", "beta"), digest: "e", installable: false, included: true, reason: "included" },
      { manifest: client("gamma-api", "gamma"), digest: "f", installable: false, included: true, reason: "included" },
      // A client a signed cut offers as installable is an ordinary option.
      { manifest: client("delta-api", "delta"), digest: "g", installable: true, included: false, reason: null },
    ],
    plugins, operations: [], roster_generation: 1, roster_revision: null,
  };
}
const roster = (loaded: boolean, present: string[] = []): RosterSnapshot =>
  ({ status: loaded ? "ready" : "loading", loaded, engines: present.map(id => ({ id, present: true }) as EngineInfo), problems: [], families: [] });
const ids = (list: { manifest: PluginManifest }[]) => list.map(c => c.manifest.identity.id);

describe("agentPicker (#2245)", () => {
  it("offers only installable non-included entries; groups the rest", () => {
    const p = agentPicker(catalog(), roster(true));
    expect(ids(p.offered)).toEqual(["alpha", "delta-api"]);
    expect(ids(p.blocked)).toEqual(["beta", "gamma"]);
    expect(ids(p.included)).toEqual(["alpha-api", "beta-api", "gamma-api"]);
  });

  it("names the source an included agent needs, by its catalog label", () => {
    const p = agentPicker(catalog(), roster(true));
    expect(p.needs.map(n => [n.entry.manifest.identity.id, n.source])).toEqual([["beta-api", "Label beta"], ["gamma-api", "Label gamma"]]);
  });

  it("a source on this host or saved as an installation needs nothing", () => {
    const saved = [{ id: "gamma", active: null, candidate: null, enabled: false, generations: [] }];
    const p = agentPicker(catalog(saved), roster(true, ["beta"]));
    expect(p.needs).toEqual([]);
  });

  it("says nothing about sources before the roster has loaded", () => {
    expect(agentPicker(catalog(), roster(false)).needs).toEqual([]);
  });

  it("an empty catalog offers nothing", () => {
    expect(agentPicker(null, roster(true))).toEqual({ offered: [], blocked: [], included: [], needs: [] });
  });
});
