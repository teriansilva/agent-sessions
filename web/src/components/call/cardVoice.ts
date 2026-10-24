/** The words a call puts on HQ's cards (#1397 P2): what to say, and what a voice settlement did. */
import type { CardSettlement } from "../../types/voice";

const ORDINAL = ["first", "second", "third", "fourth"];

/** What to say to confirm the card at `index` of `count` cards in the spoken window. */
export function confirmPhrase(index: number, count: number): string {
  return count > 1 ? `confirm the ${ORDINAL[index] ?? String(index + 1)}` : "confirm";
}

const FINAL_WORD: Record<string, string> = {
  started: "✓ Started",
  done: "✓ Done",
  partly_done: "Partly done",
  failed: "Failed",
  stale: "Changed — not run",
  interrupted: "Interrupted",
  unconfirmed: "Unconfirmed",
  dismissed: "Dismissed",
};

/** The stamp for a voice settlement, from the route's own answer. */
export function settledWord(st: CardSettlement, kind: string): { text: string; tone: "ok" | "warn" | "alert" } {
  if (st.final !== undefined) {
    if (st.final === null) return { text: "Still running", tone: "warn" };
    const text = FINAL_WORD[st.final] ?? st.final;
    return { text, tone: st.final === "started" || st.final === "done" ? "ok" : "alert" };
  }
  if (!st.ok) {
    if (st.status === 409) return { text: "Changed — not sent", tone: "alert" };
    return { text: st.status ? `Didn't go through · ${st.status}` : "Didn't go through", tone: "alert" };
  }
  if (st.status === 202) return { text: "Accepted · starting", tone: "warn" };
  return { text: kind === "nudge" || kind === "needs_you" ? "✓ Sent by voice" : "✓ Done by voice", tone: "ok" };
}
