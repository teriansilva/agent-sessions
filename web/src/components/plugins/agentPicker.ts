import type { RosterSnapshot } from "../../app/engineRoster";
import type { PluginCatalog } from "../../types/plugins";

type Entry = PluginCatalog["catalog"][number];

/** What the Add-agent picker shows (#2245), read from the catalog the server sent. The server
 *  alone decides what is installable; this only sorts its answer for display.
 *
 *  - `offered`: what this host can install — the `<select>`'s options.
 *  - `blocked`: catalog agents it cannot install, each with the server's `reason`. A disabled
 *    option can never be picked, so its reason is shown beside the picker, not on selection.
 *  - `included`: the API agents BattleLab includes. They are never installed here (they use their
 *    source agent's installation), so they are named, not offered.
 *  - `needs`: the included agents whose source agent is neither installed nor installable, with
 *    the source's label. Only once the roster has loaded: before that, a host binary is unknown. */
export function agentPicker(catalog: PluginCatalog | null, roster: RosterSnapshot) {
  const entries = catalog?.catalog ?? [];
  const label = (id: string) => entries.find(c => c.manifest.identity.id === id)?.manifest.identity.label ?? id;
  const available = (id: string) =>
    entries.some(c => c.manifest.identity.id === id && !c.included && c.installable !== false)
    || !!catalog?.plugins.some(p => p.id === id)
    || roster.engines.some(e => e.id === id && e.present);
  const included = entries.filter(c => c.included);
  const needs: { entry: Entry; source: string }[] = roster.loaded
    ? included.flatMap(c => {
      const source = c.manifest.api?.source;
      return source && source !== c.manifest.identity.id && !available(source) ? [{ entry: c, source: label(source) }] : [];
    })
    : [];
  return {
    offered: entries.filter(c => !c.included && c.installable !== false),
    blocked: entries.filter(c => !c.included && c.installable === false),
    included,
    needs,
  };
}
