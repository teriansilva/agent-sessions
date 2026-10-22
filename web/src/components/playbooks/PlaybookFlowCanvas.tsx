/** Accessible workflow graph. Selection/layout stay outside the saved bundle. */
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
import "../hud/reactFlow.css";
import { useIsMobile } from "../../lib/useIsMobile";
import { useTheme } from "../../theme/themeStore";
import type { Step } from "./playbookDraft";
import { PlaybookActor } from "./PlaybookCard";
import { dependencyError, graphLayout, isNote } from "./playbookGraph";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbookCanvas.module.css";

type StepNode = Node<{ step: Step; index: number; editable: boolean }, "step">;
function FlowStep({ data, selected }: NodeProps<StepNode>) {
  const { step, index, editable } = data;
  return (
    <div className={styles.node} data-selected={selected}>
      <Handle
        type="target"
        id="after"
        position={Position.Left}
        isConnectable={editable}
        aria-label={`Prerequisite input for ${step.title}`}
      />
      <span className={styles.meta}>Step {index + 1}</span>
      <strong>{step.title}</strong>
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
          : `${step.checklist?.length ?? 0} completion rules`}
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
  selectionEpoch?: number;
  onSelect?: (id: string) => void;
  onBackground?: () => void;
  onChange?: (steps: Step[]) => void;
};

export function PlaybookFlowCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}

function Canvas({
  steps,
  selected,
  selectionEpoch = 0,
  onSelect,
  onBackground,
  onChange,
}: Props) {
  const rf = useReactFlow<StepNode>();
  const mobile = useIsMobile();
  const { theme } = useTheme(); // React Flow's colorMode follows the theme in force (#1531)
  const [navigationStep, setNavigationStep] = useState(steps[0]?.id ?? "");
  const [positions, setPositions] = useState<Record<string, XYPosition>>({});
  const [sizes, setSizes] = useState<
    Record<string, { width: number; height: number }>
  >({});
  const [message, setMessage] = useState("");
  const [edgeSelection, setEdgeSelection] = useState({
    id: "",
    epoch: selectionEpoch,
  });
  const edgeId = edgeSelection.epoch === selectionEpoch ? edgeSelection.id : "";
  const setEdgeId = (id: string) =>
    setEdgeSelection({ id, epoch: selectionEpoch });
  const layout = useMemo(() => graphLayout(steps), [steps]);
  const nodes: StepNode[] = steps.map((step, index) => ({
    id: step.id,
    type: "step",
    data: { step, index, editable: !!onChange },
    position: positions[step.id] ?? layout.positions.get(step.id)!,
    measured: sizes[step.id],
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
  const navigationIndex = Math.max(
    0,
    steps.findIndex((s) => s.id === (selected || navigationStep)),
  );
  function showNode(index: number) {
    const step = steps[index];
    if (!step) return;
    setNavigationStep(step.id);
    const node = rf.getNode(step.id);
    if (!node) return;
    void rf.setCenter(
      node.position.x + (node.measured?.width ?? 224) / 2,
      node.position.y + (node.measured?.height ?? 136) / 2,
      { zoom: Math.max(0.75, rf.getZoom()) },
    );
  }
  return (
    <div
      className={styles.root}
      onKeyDownCapture={(event) => {
        if (event.key === "Escape") setEdgeId("");
        if (
          event.key === " " &&
          (event.target as HTMLElement).closest(".react-flow__node")
        )
          event.preventDefault();
      }}
    >
      <div className={styles.toolbar} aria-label="Flowchart controls">
        <button
          type="button"
          className={buttons.ghost}
          onClick={() => {
            setPositions({});
            requestAnimationFrame(
              () =>
                void rf.fitView({
                  padding: 0.2,
                  minZoom: mobile ? 0.75 : 0.25,
                }),
            );
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
          onClick={() =>
            void rf.fitView({ padding: 0.2, minZoom: mobile ? 0.75 : 0.25 })
          }
        >
          Fit flow
        </button>
        {mobile && steps.length > 1 && (
          <>
            <button
              type="button"
              className={buttons.ghost}
              disabled={navigationIndex === 0}
              onClick={() => showNode(navigationIndex - 1)}
            >
              Show previous step
            </button>
            <button
              type="button"
              className={buttons.ghost}
              disabled={navigationIndex === steps.length - 1}
              onClick={() => showNode(navigationIndex + 1)}
            >
              Show next step
            </button>
          </>
        )}
      </div>
      <div
        className={styles.canvas}
        aria-label="Playbook flowchart"
        data-testid="playbook-flowchart"
      >
        {steps.length ? (
          <ReactFlow<StepNode>
            colorMode={theme}
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodeClick={(_, n) => {
              setNavigationStep(n.id);
              onSelect?.(n.id);
              setEdgeId("");
            }}
            onEdgeClick={(_, e) => {
              if (e.type === "rework") onSelect?.(e.source);
              else setEdgeId(e.id);
            }}
            onPaneClick={() => {
              setEdgeId("");
              onBackground?.();
            }}
            onNodesChange={(changes) => {
              for (const c of changes)
                if (c.type === "select" && c.selected) {
                  setNavigationStep(c.id);
                  onSelect?.(c.id);
                }
              // Controlled nodes must retain their measured size across parent renders.
              // Otherwise React Flow briefly hides them, dropping restored keyboard focus.
              const dimensions = changes.filter(
                (c) => c.type === "dimensions" && c.dimensions,
              );
              if (dimensions.length)
                setSizes((old) => {
                  let next = old;
                  for (const c of dimensions) {
                    if (
                      c.type !== "dimensions" ||
                      !c.dimensions?.width ||
                      !c.dimensions.height
                    )
                      continue;
                    if (
                      old[c.id]?.width === c.dimensions.width &&
                      old[c.id]?.height === c.dimensions.height
                    )
                      continue;
                    if (next === old) next = { ...old };
                    next[c.id] = c.dimensions;
                  }
                  return next;
                });
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
            fitViewOptions={{
              padding: 0.2,
              minZoom: mobile ? 0.75 : 0.25,
              nodes: mobile ? [{ id: steps[0].id }] : undefined,
            }}
            minZoom={mobile ? 0.75 : 0.25}
            maxZoom={1.5}
            deleteKeyCode={null}
            multiSelectionKeyCode={null}
            selectionKeyCode={null}
            nodesConnectable={!!onChange}
            nodesDraggable
            selectNodesOnDrag={false}
            ariaLabelConfig={{
              "node.a11yDescription.default": onSelect
                ? "Press Enter or Space to open step Properties. Escape returns to workflow properties. Dragging changes layout only."
                : "This is a read-only workflow preview.",
            }}
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
          The draft has a dependency cycle. Use the step Properties to resolve
          it before saving.
        </p>
      )}
      {layout.missing && (
        <p role="alert">
          Some connections refer to removed steps. Their references are
          retained; correct them in the step Properties before saving.
        </p>
      )}
      {message && <p role="status">{message}</p>}
      <p className={styles.hint}>
        {onChange
          ? "Connect a step’s right handle to another step’s left handle to add a dependency. Select a line to remove it. Configure rework in step Properties, or use Starts after and the step actions without dragging."
          : "Arrows show dependencies; dashed return paths show bounded rework."}{" "}
        {onSelect &&
          "Select a step with Enter or Space; Escape returns to workflow properties. "}
        Dragging changes layout only.
      </p>
    </div>
  );
}
