/** The Memory graph (#1441): notes by type, `[[links]]` between them (dashed), and the sessions
 *  that wrote them (solid, in the engine's accent). Lazy-loaded with the page so ReactFlow stays
 *  out of the main bundle. Clicking a note selects it; clicking a resolved session opens it. */
import { useEffect, useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useNavigate } from "react-router-dom";
import { engineColor, engineName, useEngineRoster } from "../../app/engineRoster";
import { relTime, sessionPathFromKey } from "../../lib/format";
import type { MemoryNote } from "../../types/memory";
import { memoryGraph, TYPE_TAG, type GraphSession } from "./memoryGraph";
import styles from "./memoryGraph.module.css";

type NoteData = { note: MemoryNote; selected: boolean };
type SessionData = { session: GraphSession };
type NoteNode = Node<NoteData, "note">;
type SessionNode = Node<SessionData, "session">;

function NoteCard({ data }: NodeProps<NoteNode>) {
  const { note, selected } = data;
  return (
    <div className={styles.note} data-selected={selected} data-testid={`memory-node-${note.slug}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className={styles.tag} data-type={note.type}>{TYPE_TAG[note.type]}</span>
      <strong className={styles.title}>{note.name}</strong>
      <span className={styles.meta}>
        {note.session.engine ? engineName(note.session.engine) : "unknown"}
        {note.session.kind === "import" ? " (imported)" : note.session.resolved ? "" : " (unresolved)"} · {relTime(note.updated)}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

function SessionCard({ data }: NodeProps<SessionNode>) {
  const { session, notes } = data.session;
  return (
    <div className={styles.session} data-testid="memory-session-node" style={{ color: engineColor(session.engine) }}>
      <span className={styles.engine}>
        {engineName(session.engine) || "unknown"} · {session.key ? session.key.split(":")[1]?.slice(0, 8) : session.kind === "import" ? "its own memory" : "session unresolved"}
      </span>
      <span className={styles.kind}>
        {(session.kind || "agent").toUpperCase()} · {notes} {notes === 1 ? "NOTE" : "NOTES"}
      </span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { note: NoteCard, session: SessionCard };

type Props = { notes: MemoryNote[]; selected: string | null; onSelect: (slug: string) => void };

export default function MemoryGraph(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

function Canvas({ notes, selected, onSelect }: Props) {
  useEngineRoster(); // re-render once the roster (engine names and accents) arrives
  const navigate = useNavigate();
  const graph = useMemo(() => memoryGraph(notes), [notes]);
  const built = useMemo<(NoteNode | SessionNode)[]>(
    () =>
      graph.nodes.map(n =>
        n.kind === "note"
          ? { id: n.id, type: "note", position: { x: n.x, y: n.y }, data: { note: n.note!, selected: false }, ariaLabel: `Note: ${n.note!.name}` }
          : { id: n.id, type: "session", position: { x: n.x, y: n.y }, data: { session: n.session! }, ariaLabel: `Session ${n.session!.session.engine}` },
      ),
    [graph],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState<NoteNode | SessionNode>(built);
  useEffect(() => setNodes(built), [built, setNodes]);
  // Selection changes only the data flag, so measured sizes survive and nothing re-lays out.
  useEffect(() => {
    setNodes(ns =>
      ns.map(n => (n.type === "note" ? { ...n, data: { ...n.data, selected: (n.data as NoteData).note.slug === selected } } : n)),
    );
  }, [selected, setNodes]);
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map(e => {
        const from = graph.nodes.find(n => n.id === e.source);
        const stroke = e.kind === "wrote" ? engineColor(from?.session?.session.engine ?? "") : "var(--text-3)";
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          type: "smoothstep",
          ariaLabel: e.kind === "wrote" ? "wrote" : "links to",
          style: { stroke, strokeWidth: 1.4, strokeDasharray: e.kind === "links" ? "5 4" : undefined, opacity: 0.85 },
          markerEnd: e.kind === "links" ? { type: MarkerType.ArrowClosed, color: "var(--text-3)" } : undefined,
        };
      }),
    [graph],
  );
  return (
    <div className={styles.canvas} data-testid="memory-graph" aria-label="Memory graph">
      <ReactFlow<NoteNode | SessionNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.25}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, n) => {
          if (n.type === "note") onSelect((n.data as NoteData).note.slug);
          else {
            const to = sessionPathFromKey((n.data as SessionData).session.session.key);
            if (to) navigate(to);
          }
        }}
      >
        <Background variant={BackgroundVariant.Lines} gap={48} color="var(--line)" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
      <div className={styles.legend}>
        <span><i className={styles.solid} /> wrote</span>
        <span><i className={styles.dashed} /> links to</span>
        <span className={styles.dim}>Reads aren't drawn — BattleLab can't see who read a file.</span>
      </div>
    </div>
  );
}
