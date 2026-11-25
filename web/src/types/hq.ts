/** One thinking vocabulary; `hq` inherits a field, `auto` selects provider policy. */
export type HqThinking = "hq" | "auto" | "off" | "low" | "medium" | "high";
export interface HqModelChoice { model: string; thinking: HqThinking }
export interface HqModelSettings { typed: HqModelChoice; mission: HqModelChoice }
