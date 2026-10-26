import type { StructuredTool } from "../../types/api";

/** One stretch of the agent's text and the calls it made after it. `message`: which agent message
 *  (#1643) the stretch belongs to, counted from the first in the reply; 0 without boundaries. */
export interface ReplyPart {
  text: string;
  tools: StructuredTool[];
  message: number;
}

/** A turn's reply cut at its tool calls, so the activity reads in the order it happened: `lead`
 *  ran before the agent wrote anything, each part is text followed by the calls made after it.
 *  With `breaks` (#1643, where each message starts) it is also cut where a message starts, so a
 *  text-only run of progress notes is one part per note. Null when the reply is empty or a call
 *  carries no place (an older server, a public link): the activity then reads as one block before
 *  the reply, as it always did. */
export function interleave(
  reply: string | null | undefined,
  tools: StructuredTool[],
  breaks?: number[],
): { lead: StructuredTool[]; parts: ReplyPart[] } | null {
  if (!reply || !tools.every((t) => typeof t.at === "number")) return null;
  const starts = [...new Set(breaks ?? [])].filter((b) => b > 0 && b < reply.length).sort((a, b) => a - b);
  const lead: StructuredTool[] = [];
  const parts: ReplyPart[] = [];
  let from = 0;
  let message = 0;
  const cut = (to: number) => {
    // Blank text between two calls (a message that was only a line break) keeps them together.
    if (reply.slice(from, to).trim()) {
      parts.push({ text: reply.slice(from, to), tools: [], message });
      from = to;
    }
  };
  const breakUpTo = (to: number) => {
    while (starts.length && starts[0] <= to) {
      cut(starts.shift()!);
      message += 1;
    }
  };
  for (const tool of [...tools].sort((a, b) => a.at! - b.at!)) {
    const at = Math.min(Math.max(tool.at!, from), reply.length);
    breakUpTo(at);
    cut(at);
    (parts.length ? parts[parts.length - 1].tools : lead).push(tool);
  }
  breakUpTo(reply.length);
  if (reply.slice(from).trim() || !parts.length) parts.push({ text: reply.slice(from), tools: [], message });
  return { lead, parts };
}

/** Fold a long turn's older loaded messages (#1643): above `over` messages, all but the newest
 *  `keep` (with the calls made after them) go behind one disclosure. The newest — a finished
 *  turn's final reply among them — always stay out. Null when there is nothing to fold. */
export const FOLD_OVER = 8;
export const FOLD_KEEP = 3;
export function fold(parts: ReplyPart[], over = FOLD_OVER, keep = FOLD_KEEP) {
  const messages = new Set(parts.map((p) => p.message)).size;
  if (messages <= over) return null;
  const firstShown = [...new Set(parts.map((p) => p.message))].sort((a, b) => a - b)[messages - keep];
  return {
    count: messages - keep,
    folded: parts.filter((p) => p.message < firstShown),
    shown: parts.filter((p) => p.message >= firstShown),
  };
}
