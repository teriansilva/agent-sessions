import { render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { expect, test } from "vitest";
import { projectColor } from "../../lib/format";
import type { Session } from "../../types/api";
import { SessionNode } from "./SessionNode";

const sess = (over: Partial<Session> = {}): Session =>
  ({
    id: "claude:u1",
    engine: "claude",
    uuid: "u1",
    short_uuid: "u1",
    cwd: "/p",
    project: { kind: "folder" as const, id: "/p", name: "p" },
    last_mtime: 0,
    first_user_message: "",
    title: "My session",
    sticky: false,
    archived: false,
    ...over,
  }) as Session;

const renderNode = (data: object) =>
  render(<SessionNode {...({ data } as unknown as NodeProps)} />);

// The chip is presentational — the click that opens the session is handled by the canvas's
// React Flow onNodeClick (#149). Here we verify the chip carries nodrag/nopan (so a press
// doesn't pan) and renders the title/engine.
test("chip carries nopan (not nodrag, so it can be dragged) + renders title and engine", () => {
  renderNode({ session: sess(), active: true, selected: false });
  const chip = screen.getByLabelText(/open my session/i);
  // nopan stops a press from panning the canvas; nodrag is intentionally absent so React Flow
  // can drag the chip to reassign it (#424 Phase 5).
  expect(chip.className).toMatch(/\bnopan\b/);
  expect(chip.className).not.toMatch(/\bnodrag\b/);
  expect(screen.getByText("My session")).toBeInTheDocument();
  expect(screen.getByText("cc")).toBeInTheDocument();
});

test("the open session's chip is marked selected/aria-current (#149)", () => {
  renderNode({ session: sess(), active: false, selected: true });
  const chip = screen.getByLabelText(/open my session/i);
  expect(chip).toHaveAttribute("aria-current", "true");
  expect(chip.className).toMatch(/\bselected\b/);
});

// #156: when the server reports the agent is currently working, the chip's dot picks up
// the .working modifier (which the CSS animates) and exposes role="status" for SR.
test("working session chip pulses + announces status (#156)", () => {
  const { container } = renderNode({
    session: sess(),
    active: true,
    working: true,
    selected: false,
  });
  const dot = container.querySelector(".tr-ov-dot");
  expect(dot).not.toBeNull();
  expect(dot?.className).toMatch(/\bworking\b/);
  expect(dot).toHaveAttribute("aria-label", "agent working");
  expect(dot).toHaveAttribute("role", "status");
});

test("non-working chip falls back to active/idle (no pulse) (#156)", () => {
  const { container } = renderNode({
    session: sess(),
    active: true,
    working: false,
    selected: false,
  });
  const dot = container.querySelector(".tr-ov-dot");
  expect(dot?.className).toMatch(/\bactive\b/);
  expect(dot?.className).not.toMatch(/\bworking\b/);
  expect(dot).not.toHaveAttribute("role");
});

// ---- list-row parity (#424 Phase 4) -------------------------------------------

test("an intervention-required chip shows the ! badge with its reason (#356 parity)", () => {
  renderNode({
    session: sess({
      intervention_required: true,
      intervention_reason: "needs input",
    }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "p",
  });
  const badge = screen.getByRole("img", {
    name: /intervention required: needs input/i,
  });
  expect(badge).toHaveTextContent("!");
});

test("a review-excluded chip suppresses the ! badge and shows the exclusion marker (#424)", () => {
  renderNode({
    session: sess({
      intervention_required: true,
      review_excluded: true,
      ai_summary: "stale",
    }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "p",
  });
  expect(
    screen.queryByRole("img", { name: /intervention required/i }),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/excluded from ai review/i)).toBeInTheDocument();
  expect(screen.queryByText("stale")).not.toBeInTheDocument();
});

test("renders the AI summary line (#356 parity)", () => {
  renderNode({
    session: sess({ ai_summary: "Refactoring the parser" }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "p",
  });
  expect(screen.getByText("Refactoring the parser")).toBeInTheDocument();
});

test("an assigned chip shows BOTH the project entity and its launch folder (#424 parity)", () => {
  renderNode({
    session: sess({
      project: {
        kind: "project" as const,
        id: "p-1",
        name: "Cayoo",
        color: "#5fd7ff",
      },
    }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "~/work/api",
  });
  expect(screen.getByText("Cayoo")).toBeInTheDocument();
  expect(screen.getByText(/~\/work\/api/)).toBeInTheDocument();
});

test("an unassigned chip shows the launch folder only, no project chip (#424 parity)", () => {
  const { container } = renderNode({
    session: sess(),
    active: true,
    working: false,
    selected: false,
    folderLabel: "~/claude",
  });
  expect(screen.getByText(/~\/claude/)).toBeInTheDocument();
  expect(container.querySelector(".tr-ov-chip-proj")).toBeNull();
});

// #285: the chip publishes the project accent as --proj (explicit entity colour, else the
// stable ref-key hash) so the foot's project dot always renders for assigned sessions.
test("chip carries --proj: explicit entity colour wins, uncoloured entities hash their id (#285)", () => {
  const { container } = renderNode({
    session: sess({
      project: {
        kind: "project" as const,
        id: "p-1",
        name: "Cayoo",
        color: "#5fd7ff",
      },
    }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "p",
  });
  const chip = container.querySelector(".tr-ov-chip") as HTMLElement;
  expect(chip.style.getPropertyValue("--proj")).toBe("#5fd7ff");
  expect(container.querySelector(".tr-ov-proj-dot")).not.toBeNull();

  const { container: c2 } = renderNode({
    session: sess({
      project: { kind: "project" as const, id: "p-2", name: "Two" },
    }),
    active: true,
    working: false,
    selected: false,
    folderLabel: "p",
  });
  const chip2 = c2.querySelector(".tr-ov-chip") as HTMLElement;
  expect(chip2.style.getPropertyValue("--proj")).toBe(projectColor("p-2"));
  expect(c2.querySelector(".tr-ov-proj-dot")).not.toBeNull();
});

// #284: the server resolves the meaningful display title (manual rename → AI title →
// meaningful first message, else ""). When it's "" the chip must drop to the short id —
// NEVER fall back to the raw first message, or a stray "a" / "." would leak as the name.
test("a chip with an empty title falls back to the short id, never the raw first message (#284)", () => {
  renderNode({
    session: sess({ title: "", first_user_message: "a", short_uuid: "u1abc" }),
    active: true,
    selected: false,
    folderLabel: "~/claude",
  });
  expect(screen.getByText("u1abc")).toBeInTheDocument();
  expect(screen.queryByText("a")).not.toBeInTheDocument();
});
