/** What the launch confirmation says about the objective set (#1061 Phase 3). Pure, so each branch is
 *  a table test.
 *
 *  The confirmation is shown before EVERY launch — nothing runs without the operator's Confirm begin —
 *  so "ask only when ambiguous" becomes "say why it is worth a look only when it is": the issue's
 *  predicate, verbatim. Any one of (1) a suggestion was dropped, (2) a target was filled in from the
 *  instruction, (3) nothing gates completion. Mechanical, never a model judgement. */
import type { MissionObjective } from "../../types/api";

export interface Fit {
  dropped: number;
  parameterised: number;
}

/** The sentences under the list, empty when the set is ordinary. */
export function worthALook(
  fit: Fit | undefined,
  objectives: MissionObjective[],
): string[] {
  const out: string[] = [];
  const p = fit?.parameterised ?? 0;
  const d = fit?.dropped ?? 0;
  if (p > 0)
    out.push(
      `${p} ${p === 1 ? "objective was" : "objectives were"} fitted to targets named in your instruction — check they are the right ones.`,
    );
  if (d > 0)
    out.push(
      `${d} ${d === 1 ? "suggestion" : "suggestions"} did not fit the checklist and ${d === 1 ? "was" : "were"} dropped.`,
    );
  if (objectives.length > 0 && !objectives.some((o) => o.gate))
    out.push(
      "Nothing here is required, so the mission can never confirm itself finished — you close it.",
    );
  return out;
}

/** `forge_merged · devopsagent/alpha`, `note · not checked`: the probe and the target it checks. */
export function checksWhat(o: MissionObjective): string {
  if (o.probe === "none") return "note · not checked";
  const a = o.probe_args ?? {};
  const target = ["repo", "branch", "url"]
    .map((k) => a[k])
    .filter((v): v is string => typeof v === "string" && v.length > 0);
  return [o.probe, ...target].join(" · ");
}
