/** View-only grouping: acknowledged steering input belongs before the response it steers.
 * The journal stays chronological; queued/uncertain input and omitted targets stay in place. */
import type { StructuredAgentTurn, StructuredTurn } from "../../types/api";
import { isAgentTurn } from "./structuredView";

export interface ConversationGroup {
  turn: StructuredTurn | StructuredAgentTurn;
  additions: StructuredTurn[];
}

export function conversationGroups(turns: (StructuredTurn | StructuredAgentTurn)[]): ConversationGroup[] {
  const groups: ConversationGroup[] = [];
  const targets = new Map<string, ConversationGroup>();
  for (const turn of turns) {
    const target = !isAgentTurn(turn) && turn.state === "delivered" && turn.delivery === "steered"
      && turn.delivery_turn_id ? targets.get(turn.delivery_turn_id) : undefined;
    if (target && !isAgentTurn(turn)) {
      target.additions.push(turn);
    } else {
      const group = { turn, additions: [] };
      groups.push(group);
      // Only an earlier response-bearing turn can own additions, never another delivered input.
      // This also keeps missing, self-referencing and forward-referencing records visible.
      if (isAgentTurn(turn) || turn.state !== "delivered") targets.set(turn.turn_id, group);
    }
  }
  return groups;
}
