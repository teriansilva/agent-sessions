import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { NodeProps } from "@xyflow/react";
import { MemoryRouter, useLocation } from "react-router-dom";
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

function renderNode(data: object) {
  const loc: { pathname?: string } = {};
  function Probe() {
    loc.pathname = useLocation().pathname;
    return null;
  }
  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <SessionNode {...({ data } as unknown as NodeProps)} />
      <Probe />
    </MemoryRouter>,
  );
  return loc;
}

test("chip is nodrag/nopan and opens the session on click (#149)", async () => {
  const loc = renderNode({ session: sess(), active: true, selected: false });
  const btn = screen.getByRole("button", { name: /open my session/i });
  // Without these RF would swallow the click as a pan/drag.
  expect(btn.className).toMatch(/\bnodrag\b/);
  expect(btn.className).toMatch(/\bnopan\b/);
  await userEvent.click(btn);
  expect(loc.pathname).toBe("/s/claude/u1");
});

test("the open session's chip is marked selected/aria-current (#149)", () => {
  renderNode({ session: sess(), active: false, selected: true });
  const btn = screen.getByRole("button");
  expect(btn).toHaveAttribute("aria-current", "true");
  expect(btn.className).toMatch(/\bselected\b/);
});
