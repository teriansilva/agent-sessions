import type { SessionDriver } from "../../types/api";

/** The words a driver badge says (#2398): HQ and which task, You, or Mission. */
export function driverLabel(d: SessionDriver): string {
  if (d.who === "hq") return d.task_label ? `HQ · ${d.task_label}` : "HQ";
  if (d.who === "mission") return "Mission";
  return "You";
}
