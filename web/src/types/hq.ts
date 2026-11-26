/** One thinking vocabulary; `hq` inherits a field, `auto` selects provider policy. */
export type HqThinking = "hq" | "auto" | "off" | "low" | "medium" | "high";
export interface HqModelChoice { model: string; thinking: HqThinking }
export interface HqModelSettings { typed: HqModelChoice; mission: HqModelChoice }

/** Settings → AI → HQ → Authority (#2390, epic #2070 §1). */
export type HqAuthorityClass = "reversible" | "starts" | "destructive" | "external" | "widens";
export type HqAuthorityValue = "ask" | "act";
export type HqAuthorityPreset = "ask_first" | "standard" | "full" | "custom";
export interface HqAuthority {
  classes: Record<HqAuthorityClass, HqAuthorityValue>;
  /** Derived from `classes`: a mix that matches no preset is `custom`. */
  preset: HqAuthorityPreset;
  revision: number;
  /** The classes HQ can act on in this version; a stored `act` for any other is kept, not run. */
  actable: HqAuthorityClass[];
  risky: HqAuthorityClass[];
}
export type HqAuthorityPatch =
  | { revision: number; preset: Exclude<HqAuthorityPreset, "custom"> }
  | { revision: number; classes: Partial<Record<HqAuthorityClass, HqAuthorityValue>> };
/** One act HQ made on its own (#2390): read back from the acts log. */
export interface HqAct {
  ts: number;
  op: string;
  label: string;
  title: string;
  state: string;
  outcome: string;
  via: "typed" | "call";
  conversation: string;
}
