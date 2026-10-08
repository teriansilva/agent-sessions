/** A second view of the editor's draft. Dragging never writes layout into a bundle. */
import { useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
  type XYPosition,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Step } from "./playbookDraft";
import { PlaybookActor } from "./PlaybookCard";
import { dependencyError, graphLayout, isNote } from "./playbookGraph";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookCanvas.module.css";

type StepNode = Node<{ step: Step; editable: boolean }, "step">;
function FlowStep({ data, selected }: NodeProps<StepNode>) {
  const { step, editable } = data;
  return (
    <div className={styles.node} data-selected={selected}>
      <Handle
        type="target"
        id="after"
        position={Position.Left}
        isConnectable={editable}
        aria-label={`Prerequisite input for ${step.title}`}
      />
      <strong>{step.title}</strong>
      <span className={styles.id}>{step.id}</span>
      <PlaybookActor
        step={{
          ...step,
          actor: step.actor as Parameters<
            typeof PlaybookActor
          >[0]["step"]["actor"],
          after: step.after ?? [],
          note: isNote(step),
        }}
      />
      <span className={styles.meta}>
        {isNote(step)
          ? "Note · gates nothing"
          : `${step.checklist?.length ?? 0} evidence checks`}
      </span>
      {step.memory && step.memory !== "none" && (
        <span className={styles.meta}>Memory: {step.memory}</span>
      )}
      <Handle
        type="source"
        id="next"
        position={Position.Right}
        isConnectable={editable && !isNote(step)}
        aria-label={`Next step from ${step.title}`}
      />
      <Handle
        type="source"
        id="rework-from"
        position={Position.Top}
        isConnectable={false}
        className={styles.reworkHandle}
      />
      <Handle
        type="target"
        id="rework-to"
        position={Position.Top}
        isConnectable={false}
        className={styles.reworkHandle}
      />
    </div>
  );
}

function ReworkEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  label,
  markerEnd,
}: EdgeProps) {
  const y = Math.min(sourceY, targetY) - 70;
  const path = `M ${sourceX},${sourceY} C ${sourceX},${y} ${targetX},${y} ${targetX},${targetY}`;
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerEnd={markerEnd}
        style={{ stroke: "var(--text-2)", strokeDasharray: "6 4" }}
      />
      <EdgeLabelRenderer>
        <span
          className={`${styles.edgeLabel} nodrag nopan`}
          style={{
            transform: `translate(-50%, -100%) translate(${(sourceX + targetX) / 2}px,${y + 14}px)`,
          }}
        >
          {label}
        </span>
      </EdgeLabelRenderer>
    </>
  );
}
const nodeTypes = { step: FlowStep };
const edgeTypes = { rework: ReworkEdge };
type Props = {
  steps: Step[];
  selected?: string;
  onSelect?: (id: string) => void;
  onChange?: (steps: Step[]) => void;
};

export function PlaybookFlowCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

function Canvas({ steps, selected, onSelect, onChange }: Props) {
  const rf = useReactFlow<StepNode>();
  const [positions, setPositions] = useState<Record<string, XYPosition>>(() =>
    Object.fromEntries(graphLayout(steps).positions),
  );
  const [message, setMessage] = useState("");
  const [edgeId, setEdgeId] = useState("");
  const layout = useMemo(() => graphLayout(steps), [steps]);
  const nodes: StepNode[] = steps.map((step) => ({
    id: step.id,
    type: "step",
    data: { step, editable: !!onChange },
    position: positions[step.id] ?? layout.positions.get(step.id)!,
    selected: selected === step.id,
    ariaLabel: `Step: ${step.title}`,
  }));
  const ids = new Set(steps.map((s) => s.id));
  const edges: Edge[] = steps.flatMap((step) => [
    ...(step.after ?? [])
      .filter((id) => ids.has(id))
      .map((source) => ({
        id: `after:${source}:${step.id}`,
        source,
        target: step.id,
        sourceHandle: "next",
        targetHandle: "after",
        type: "smoothstep",
        selected: edgeId === `after:${source}:${step.id}`,
        ariaLabel: `${steps.find((s) => s.id === source)!.title} before ${step.title}`,
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--text-2)" },
        style: { stroke: "var(--text-2)", strokeWidth: 1.5 },
      })),
    ...(step.rework && ids.has(step.rework.to)
      ? [
          {
            id: `rework:${step.id}`,
            source: step.id,
            target: step.rework.to,
            sourceHandle: "rework-from",
            targetHandle: "rework-to",
            type: "rework",
            label: `Rework: ${step.rework.when} · max ${step.rework.max_rounds}`,
            ariaLabel: `Rework from ${step.title}, at most ${step.rework.max_rounds} rounds`,
            markerEnd: { type: MarkerType.ArrowClosed, color: "var(--text-2)" },
          },
        ]
      : []),
  ]);
  const selectedEdge = edges.find(
    (e) => e.id === edgeId && e.type !== "rework",
  );
  return (
    <div className={styles.root}>
      <div className={styles.toolbar} aria-label="Flowchart controls">
        <button
          type="button"
          className={buttons.ghost}
          onClick={() => {
            setPositions(Object.fromEntries(layout.positions));
            requestAnimationFrame(() => void rf.fitView({ padding: 0.2 }));
          }}
        >
          Auto-layout
        </button>
        <button
          type="button"
          className={buttons.ghost}
          aria-label="Zoom out flowchart"
          onClick={() => void rf.zoomOut()}
        >
          −
        </button>
        <button
          type="button"
          className={buttons.ghost}
          aria-label="Zoom in flowchart"
          onClick={() => void rf.zoomIn()}
        >
          +
        </button>
        <button
          type="button"
          className={buttons.ghost}
          onClick={() => void rf.fitView({ padding: 0.2 })}
        >
          Fit flow
        </button>
      </div>
      <div
        className={styles.canvas}
        aria-label="Playbook flowchart"
        data-testid="playbook-flowchart"
      >
        {steps.length ? (
          <ReactFlow<StepNode>
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodeClick={(_, n) => {
              onSelect?.(n.id);
              setEdgeId("");
            }}
            onEdgeClick={(_, e) => {
              if (e.type === "rework") onSelect?.(e.source);
              else setEdgeId(e.id);
            }}
            onPaneClick={() => setEdgeId("")}
            onNodesChange={(changes) => {
              for (const c of changes)
                if (c.type === "select" && c.selected) onSelect?.(c.id);
              const moves = changes.filter(
                (c) => c.type === "position" && c.position,
              );
              if (moves.length)
                setPositions((old) => {
                  const next = { ...old };
                  for (const c of moves)
                    if (c.type === "position" && c.position)
                      next[c.id] = c.position;
                  return next;
                });
            }}
            onEdgesChange={(changes) => {
              for (const c of changes)
                if (c.type === "select" && c.selected) setEdgeId(c.id);
            }}
            onConnect={({ source, target, sourceHandle, targetHandle }) => {
              if (
                !onChange ||
                sourceHandle !== "next" ||
                targetHandle !== "after"
              )
                return;
              const error = dependencyError(steps, source, target);
              setMessage(error || "Dependency added to the draft.");
              if (!error)
                onChange(
                  steps.map((s) =>
                    s.id === target
                      ? { ...s, after: [...(s.after ?? []), source] }
                      : s,
                  ),
                );
            }}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.25}
            maxZoom={1.5}
            deleteKeyCode={null}
            nodesConnectable={!!onChange}
            nodesDraggable
            edgesReconnectable={false}
            panOnScroll={false}
            zoomOnScroll={false}
            proOptions={{ hideAttribution: true }}
          >
            <Background
              variant={BackgroundVariant.Dots}
              gap={22}
              size={1}
              color="var(--border)"
            />
          </ReactFlow>
        ) : (
          <p>Add a step to start the flow.</p>
        )}
      </div>
      {onChange && selectedEdge && (
        <div className={styles.toolbar}>
          <span>{selectedEdge.ariaLabel}</span>
          <button
            type="button"
            className={buttons.ghost}
            onClick={() => {
              onChange(
                steps.map((s) =>
                  s.id === selectedEdge.target
                    ? {
                        ...s,
                        after: (s.after ?? []).filter(
                          (id) => id !== selectedEdge.source,
                        ),
                      }
                    : s,
                ),
              );
              setEdgeId("");
              setMessage("Dependency removed from the draft.");
            }}
          >
            Remove dependency
          </button>
        </div>
      )}
      {layout.cyclic && (
        <p role="alert">
          The draft has a dependency cycle. Use the step inspector to resolve it
          before saving.
        </p>
      )}
      {layout.missing && (
        <p role="alert">
          Some connections refer to removed steps. Their references are
          retained; correct them in the step inspector before saving.
        </p>
      )}
      {message && <p role="status">{message}</p>}
      <p className={styles.hint}>
        {onChange
          ? "Connect a step’s right handle to another step’s left handle to add a dependency. Select a line to remove it. Configure rework in the step inspector, or use List for all controls."
          : "Arrows show dependencies; dashed return paths show bounded rework."}{" "}
        Dragging changes the layout only.
      </p>
    </div>
  );
}
