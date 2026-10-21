import { engineLabel } from "../app/engineRoster";
import type { LaunchDefaults } from "../types/api";

/** What a project's sessions start with, in words (#1508): `Codex API · gpt-5.5 · high · skip
 *  prompts`. Fields left on the global default are named as such, so the line is never empty. */
export function describeLaunchDefaults(
  d: LaunchDefaults,
  global: { engine: string | null; bypass: boolean },
): string {
  const parts = [
    d.engine ? engineLabel(d.engine) : `global agent (${global.engine ? engineLabel(global.engine) : "none"})`,
  ];
  if (d.model) parts.push(d.model);
  if (d.effort) parts.push(d.effort);
  const bypass = d.bypass ?? global.bypass;
  parts.push(
    `${bypass ? "skip prompts" : "ask before acting"}${d.bypass === undefined ? " (global)" : ""}`,
  );
  return parts.join(" · ");
}

/** A block with only the keys that hold a value — the server stores `{}` as "nothing set". */
export function cleanLaunchDefaults(d: LaunchDefaults): LaunchDefaults {
  const out: LaunchDefaults = {};
  if (d.engine) {
    out.engine = d.engine;
    if (d.model) {
      out.model = d.model;
      if (d.effort) out.effort = d.effort;
    }
  }
  if (d.bypass !== undefined) out.bypass = d.bypass;
  return out;
}
