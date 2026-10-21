/** THE ONE PLACE THE DRAG LIBRARY IS IMPORTED (#967 P3).
 *
 *  `@dnd-kit/core` + `@dnd-kit/sortable` give a sortable list pointer, touch and keyboard input and
 *  screen-reader announcements; the hand-rolled alternative (pointer capture, drop geometry,
 *  auto-scroll, announcements) is ours to maintain forever. Everything else in the app talks to
 *  this module, so replacing or removing the library is one file.
 *
 *  Three rules are decided here rather than by each caller:
 *
 *  - **A drag starts on the handle only.** The row spreads nothing; `SortableItem` hands back
 *    `handleProps` for one element. The handle CSS sets `touch-action: none`, and nothing else
 *    does, so a swipe that starts anywhere else on a row is an ordinary scroll.
 *  - **A drop reports `(from, to)` once, and only when the position changed.** The caller computes
 *    the full order and posts it; this module never talks to the server.
 *  - **Vertical only.** A list reorder has no horizontal meaning, and a row sliding sideways under a
 *    finger reads as a swipe action that does not exist.
 */
import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
  type Modifier,
  type UniqueIdentifier,
} from "@dnd-kit/core";
import {
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { type CSSProperties, type ReactNode, useMemo } from "react";

const verticalOnly: Modifier = ({ transform }) => ({ ...transform, x: 0 });

/** Four pixels before a press on the handle becomes a drag, so a tap or a click on it is not one. */
const ACTIVATION_DISTANCE = 4;

const SORTABLE_INSTRUCTIONS =
  "To reorder, press Space or Enter on the handle. Use the Up and Down arrow keys to move, " +
  "Space or Enter to drop, or Escape to cancel.";

export function SortableList({
  ids,
  label,
  onMove,
  children,
}: {
  /** The items in their CURRENT order. */
  ids: string[];
  /** How the announcements name an item. */
  label: (id: string) => string;
  /** One call per drop that changed the position, with indexes into `ids`. */
  onMove: (from: number, to: number) => void;
  children: ReactNode;
}) {
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: ACTIVATION_DISTANCE },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const accessibility = useMemo(() => {
    const n = ids.length;
    const pos = (id: UniqueIdentifier) => ids.indexOf(String(id)) + 1;
    const name = (id: UniqueIdentifier) => label(String(id));
    const announcements: Announcements = {
      onDragStart: ({ active }) =>
        `Picked up ${name(active.id)}. It is in position ${pos(active.id)} of ${n}.`,
      onDragOver: ({ active, over }) =>
        over
          ? `${name(active.id)} was moved into position ${pos(over.id)} of ${n}.`
          : `${name(active.id)} is no longer over the list.`,
      onDragEnd: ({ active, over }) =>
        over
          ? `${name(active.id)} was dropped at position ${pos(over.id)} of ${n}.`
          : `${name(active.id)} was dropped outside the list and did not move.`,
      onDragCancel: ({ active }) =>
        `Moving was cancelled. ${name(active.id)} stays in position ${pos(active.id)} of ${n}.`,
    };
    return {
      announcements,
      screenReaderInstructions: { draggable: SORTABLE_INSTRUCTIONS },
    };
  }, [ids, label]);

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    onMove(from, to);
  };

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      modifiers={[verticalOnly]}
      accessibility={accessibility}
      onDragEnd={onDragEnd}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}

/** What a row needs to take part: a node setter and a style for the row, a node setter and props
 *  for its handle. */
export interface SortableRow {
  setRow: (el: HTMLElement | null) => void;
  rowStyle: CSSProperties;
  setHandle: (el: HTMLElement | null) => void;
  handleProps: Record<string, unknown>;
  isDragging: boolean;
}

/** One sortable row, handed to `children` to draw. `disabled` withdraws both halves — it cannot be
 *  picked up or dropped on — which is what a list with a write in flight needs. A component rather
 *  than an exported hook, so this module exports components only. */
export function SortableItem({
  id,
  disabled,
  children,
}: {
  id: string;
  disabled: boolean;
  children: (row: SortableRow) => ReactNode;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id, disabled });
  return children({
    setRow: setNodeRef,
    // Translate only. The library's own transform also scales, which squashes a two-line row
    // dragged past a one-line one.
    rowStyle: {
      transform: transform
        ? `translate3d(0, ${Math.round(transform.y)}px, 0)`
        : undefined,
      transition,
    },
    setHandle: setActivatorNodeRef,
    handleProps: { ...attributes, ...listeners },
    isDragging,
  });
}
