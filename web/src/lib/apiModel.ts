/** What an API session runs (#1511) — the one resolution the session pane and the recap share.
 *
 *  Three sources, never interchangeable: the per-turn OBSERVATION (`model_effective`), the
 *  operator's REQUEST (`model_requested` / `effort_requested`, null = "agent default"), and the
 *  client's REPORT of what it runs (`model_configured` / `effort_configured`, from the session
 *  start). An observation outranks a request, which outranks a report; the report is what fills an
 *  "agent default" session that has not been observed yet. A level nobody requested and the client
 *  did not report (claude reports none) is the model's default — said so, never guessed. */
export interface ApiModelFacts {
  model_effective?: string | null;
  model_requested?: string | null;
  model_configured?: string | null;
  effort_requested?: string | null;
  effort_configured?: string | null;
}

export interface ApiModel {
  /** null = nothing known yet (a session the client has not started). */
  model: string | null;
  /** A level, or "default effort" when none was requested or reported. */
  effort: string;
  /** Nothing was requested: the agent's own default runs. */
  agentDefault: boolean;
}

export const DEFAULT_EFFORT = "default effort";

export function resolveApiModel(facts: ApiModelFacts): ApiModel {
  const level = facts.effort_requested || facts.effort_configured || null;
  return {
    model: facts.model_effective || facts.model_requested || facts.model_configured || null,
    // opencode names its default variant "default": that is no level either.
    effort: level && level !== "default" ? level : DEFAULT_EFFORT,
    agentDefault: !facts.model_requested,
  };
}
