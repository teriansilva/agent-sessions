/** What the Edit direction dialog may send, decided without a DOM (#983 P2).
 *
 *  A mission objective's direction is COPIED from its playbook when the objective is created and is
 *  never live-linked. The operator can keep that copy, copy the playbook's current direction again
 *  (`reset_direction`), write one for this mission (`set_direction`), or remove it
 *  (`clear_direction`). The ops are the server's (`missions._op_*`); this only decides which one a
 *  choice means, and `null` when the choice changes nothing, so Save is never a no-op request. */
import type { MissionObjective } from "../../types/api";

export type DirectionOp =
  | { op: "set_direction"; key: string; direction: string }
  | { op: "reset_direction"; key: string }
  | { op: "clear_direction"; key: string };

/** `keep` the playbook's copy, `write` one for this mission, or `none`. */
export type DirectionChoice = "keep" | "write" | "none";

type DirectionFields = Pick<MissionObjective, "key" | "direction" | "direction_source">;

export function hasDirection(o: Pick<MissionObjective, "direction">): boolean {
  return typeof o.direction === "string" && o.direction.trim() !== "";
}

/** The direction on this row is the playbook's copy, untouched since the mission was created or reset. */
export function isCopied(o: Pick<MissionObjective, "direction" | "direction_source">): boolean {
  return hasDirection(o) && o.direction_source === "template";
}

export function initialChoice(o: DirectionFields): DirectionChoice {
  if (!hasDirection(o)) return "none";
  return isCopied(o) ? "keep" : "write";
}

/** The op a choice sends, or `null` when it would leave the objective exactly as it is. */
export function opFor(
  choice: DirectionChoice,
  o: DirectionFields,
  draft: string,
): DirectionOp | null {
  if (choice === "keep") return null;
  if (choice === "none") {
    return hasDirection(o) ? { op: "clear_direction", key: o.key } : null;
  }
  if (!draft.trim()) return null;
  if (o.direction_source === "operator" && draft === o.direction) return null;
  return { op: "set_direction", key: o.key, direction: draft };
}
