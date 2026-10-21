/** The Memory graph as plain data (#1441): notes, the sessions that wrote them, and two kinds of
 *  edge. Kept free of React and ReactFlow so the layout is unit-testable.
 *
 *  Only WRITE edges are drawn for sessions: BattleLab can see who left a note (its inbox), but a
 *  plain file says nothing about who read it, so a "read" edge would be invented. */
import type { MemoryNote, MemorySession, NoteType } from "../../types/memory";

export const TYPE_ORDER: readonly NoteType[] = ["decision", "finding", "pitfall", "reference"];
export const TYPE_TAG: Readonly<Record<NoteType, string>> = {
  decision: "DEC",
  finding: "FND",
  pitfall: "PIT",
  reference: "REF",
};

export interface GraphSession {
  id: string;
  session: MemorySession;
  notes: number;
}
export interface GraphNode {
  id: string;
  kind: "note" | "session";
  x: number;
  y: number;
  note?: MemoryNote;
  session?: GraphSession;
}
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: "wrote" | "links";
}

const COL_W = 250;
const ROW_H = 104;
const SESSION_ROW_H = 84;

/** One node per writer: a resolved session by its key, an unresolved one per engine and launch
 *  kind (the app could not tell which session it was, so it does not pretend to). */
export function sessionId(s: MemorySession): string {
  return s.key ? `s:${s.key}` : `s:unresolved:${s.engine || "unknown"}:${s.kind}`;
}

export function memoryGraph(notes: readonly MemoryNote[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const sessions = new Map<string, GraphSession>();
  for (const n of notes) {
    const id = sessionId(n.session);
    const cur = sessions.get(id);
    if (cur) cur.notes += 1;
    else sessions.set(id, { id, session: n.session, notes: 1 });
  }
  const nodes: GraphNode[] = [...sessions.values()].map((session, i) => ({
    id: session.id,
    kind: "session",
    x: 0,
    y: i * SESSION_ROW_H,
    session,
  }));
  TYPE_ORDER.forEach((type, col) => {
    notes
      .filter(n => n.type === type)
      .forEach((note, row) => {
        nodes.push({ id: `n:${note.slug}`, kind: "note", x: (col + 1) * COL_W, y: row * ROW_H, note });
      });
  });
  const known = new Set(notes.map(n => n.slug));
  const edges: GraphEdge[] = [];
  for (const n of notes) {
    edges.push({ id: `w:${n.slug}`, source: sessionId(n.session), target: `n:${n.slug}`, kind: "wrote" });
    for (const to of n.links) {
      if (known.has(to) && to !== n.slug) {
        edges.push({ id: `l:${n.slug}>${to}`, source: `n:${n.slug}`, target: `n:${to}`, kind: "links" });
      }
    }
  }
  return { nodes, edges };
}

/** Notes that link TO a slug — the inspector's "linked from". */
export function linkedFrom(notes: readonly MemoryNote[], slug: string): MemoryNote[] {
  return notes.filter(n => n.slug !== slug && n.links.includes(slug));
}
