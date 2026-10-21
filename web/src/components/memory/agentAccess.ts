/** What the "Agent access" card (#1506 P2) says, derived from the engine roster only. */
import { isActive } from "../../app/engineRoster";
import type { EngineInfo } from "../../types/api";

export const MEMORY_TOOL_TAG = "Memory tool";
export const PARAGRAPH_TAG = "Env vars + paragraph";
export const OFF_TEXT =
  "Memory is off — new sessions get nothing. Sessions already running keep access until they restart or stop.";

/** The agents memory can reach: every active agent that runs a process (a `chat` agent runs
 *  none, a `terminal` engine has no agent behind it), in roster order. */
export function memoryAgents(engines: readonly EngineInfo[]): EngineInfo[] {
  return engines
    .filter((e) => e.kind === "agent" && e.runtime !== "chat" && isActive(e))
    .slice()
    .sort((a, b) => a.display.order - b.display.order);
}

/** Does this agent get the memory tool at launch? Exactly when its manifest declares a kind. */
export function getsMemoryTool(e: EngineInfo): boolean {
  return typeof e.tools?.mcp === "string" && e.tools.mcp.length > 0;
}

/** The instruction files the agents WITHOUT the tool read, for the paste line (roster-derived). */
export function paragraphFiles(engines: readonly EngineInfo[]): string[] {
  const files = new Set<string>();
  for (const e of engines) if (!getsMemoryTool(e)) for (const f of e.instructions?.files ?? []) files.add(f);
  return [...files].sort();
}
