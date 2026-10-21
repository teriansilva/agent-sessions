import type { StructuredTool } from "../../types/api";

/** One stretch of the agent's text and the calls it made after it. */
export interface ReplyPart {
  text: string;
  tools: StructuredTool[];
}

/** A turn's reply cut at its tool calls, so the activity reads in the order it happened: `lead`
 *  ran before the agent wrote anything, each part is text followed by the calls made after it.
 *  Null when the reply is empty or a call carries no place (an older server, a public link): the
 *  activity then reads as one block before the reply, as it always did. */
export function interleave(
  reply: string | null | undefined,
  tools: StructuredTool[],
): { lead: StructuredTool[]; parts: ReplyPart[] } | null {
  if (!reply || !tools.every((t) => typeof t.at === "number")) return null;
  const lead: StructuredTool[] = [];
  const parts: ReplyPart[] = [];
  let from = 0;
  for (const tool of [...tools].sort((a, b) => a.at! - b.at!)) {
    const at = Math.min(Math.max(tool.at!, from), reply.length);
    // Blank text between two calls (a message that was only a line break) keeps them together.
    if (reply.slice(from, at).trim()) {
      parts.push({ text: reply.slice(from, at), tools: [] });
      from = at;
    }
    (parts.length ? parts[parts.length - 1].tools : lead).push(tool);
  }
  if (reply.slice(from).trim() || !parts.length) parts.push({ text: reply.slice(from), tools: [] });
  return { lead, parts };
}
