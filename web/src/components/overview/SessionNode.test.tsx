import { render, screen } from "@testing-library/react";
import type { NodeProps } from "@xyflow/react";
import { expect, test } from "vitest";
import type { Session } from "../../types/api";
import { SessionNode } from "./SessionNode";

const sess = (over: Partial<Session> = {}): Session =>
  ({
    id: "claude:u1",
    engine: "claude",
    uuid: "u1",
    short_uuid: "u1",
    cwd: "/p",
    project: "p",
    last_mtime: 0,
    first_user_message: "",
    title: "My session",
    sticky: false,
    sort_key: 0,
    archived: false,
    ...over,
  }) as Session;

const renderNode = (data: object) =>
  render(<SessionNode {...({ data } as unknown as NodeProps)} />);

// The chip is presentational — the click that opens the session is handled by the canvas's
// React Flow onNodeClick (#149). Here we verify the chip carries nodrag/nopan (so a press
// doesn't pan) and renders the title/engine.
test("chip carries nodrag/nopan + renders title and engine badge", () => {
  renderNode({ session: sess(), active: true, selected: false });
  const chip = screen.getByLabelText(/open my session/i);
  expect(chip.className).toMatch(/\bnodrag\b/);
  expect(chip.className).toMatch(/\bnopan\b/);
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
