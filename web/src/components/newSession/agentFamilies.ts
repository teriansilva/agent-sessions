/** The New session agent picker's grouping (#2282): one entry per agent (client family). */
import { engineInfo } from "../../app/engineRoster";
import type { UnavailableClient } from "../../types/api";

/** One agent on the form (#1728 follow-up): every client of one agent — its terminal (console)
 *  client and, where one exists, its API client — is ONE tile, and the way it runs is a
 *  sub-selection under the tiles. Grouping reads the roster's `family` (an API client's
 *  `api.source`, else its own id); which client a tap on the tile selects is the caller's family
 *  decision (`familyDefaults`), never derived here. */
export interface AgentFamily {
  key: string;
  /** Clients that can start a session here, in roster order. */
  offered: string[];
  /** Clients that cannot, each with its reason — listed, never silently absent (#1311). */
  unavailable: UnavailableClient[];
}

/** The family key of a client: the roster's own grouping, else the id itself (an unknown id, or
 *  an older server that sends no `family`). */
export function familyKeyOf(id: string): string {
  return engineInfo(id)?.family || id;
}

/** Group the offered and unavailable clients by family, in the order their first client appears. */
export function groupFamilies(engines: string[], unavailable: UnavailableClient[]): AgentFamily[] {
  const byKey = new Map<string, AgentFamily>();
  const get = (key: string) => {
    let fam = byKey.get(key);
    if (!fam) {
      fam = { key, offered: [], unavailable: [] };
      byKey.set(key, fam);
    }
    return fam;
  };
  for (const id of engines) get(familyKeyOf(id)).offered.push(id);
  for (const c of unavailable) get(familyKeyOf(c.id)).unavailable.push(c);
  return [...byKey.values()];
}
