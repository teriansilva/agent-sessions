import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import { AgentAccessCard } from "./AgentAccessCard";
import { getsMemoryTool, MEMORY_TOOL_TAG, memoryAgents, OFF_TEXT, PARAGRAPH_TAG } from "./agentAccess";

const ROSTER = fixture.engines as unknown as EngineInfo[];
const PARAGRAPH = "If `$BATTLELAB_MEMORY` is set, read it.";

/** A fixture engine nobody hard-codes (the roster ratchet): proves the card follows the data. */
function eighth(over: Partial<EngineInfo>): EngineInfo {
  const base = ROSTER.find((e) => e.kind === "agent" && e.runtime === "pty")!;
  return {
    ...base,
    id: "zeta",
    label: "Zeta Agent",
    display: { ...base.display, name: "zeta", badge: "zt", order: 999 },
    ...over,
  };
}

function rows() {
  const list = screen.getByRole("list", { name: "How each agent reaches memory" });
  return within(list).getAllByRole("listitem").map((li) => ({
    name: li.firstChild?.textContent ?? "",
    tag: li.lastChild?.textContent ?? "",
  }));
}

beforeEach(() => setRoster(ROSTER));
afterEach(() => resetRoster());

test("each agent's tag comes from its manifest: the memory tool where it declares one, else the paragraph", () => {
  render(<AgentAccessCard enabled paragraph={PARAGRAPH} onCopy={() => {}} />);
  const card = screen.getByRole("region", { name: "Agent access" });
  expect(within(card).getByRole("heading", { name: "Agent access" })).toBeVisible();
  expect(card).toHaveTextContent("Agents get the BattleLab memory tool automatically when memory is on.");
  expect(card).toHaveTextContent("Sessions already running join after a restart.");
  const expected = memoryAgents(ROSTER).map((e) => ({
    name: e.label,
    tag: e.tools?.mcp ? MEMORY_TOOL_TAG : PARAGRAPH_TAG,
  }));
  expect(rows()).toEqual(expected);
  // Both kinds are present in the real roster, and only agents that run a process are listed.
  expect(expected.some((r) => r.tag === MEMORY_TOOL_TAG)).toBe(true);
  expect(expected.some((r) => r.tag === PARAGRAPH_TAG)).toBe(true);
  for (const e of ROSTER.filter((x) => x.kind !== "agent" || x.runtime === "chat")) {
    expect(rows().map((r) => r.name)).not.toContain(e.label);
  }
});

test("an eighth engine follows its own manifest, and an older server without `tools` reads as no tool", () => {
  setRoster([...ROSTER, eighth({ tools: { mcp: "codex-config-override" } })]);
  const { unmount } = render(<AgentAccessCard enabled paragraph={PARAGRAPH} onCopy={() => {}} />);
  expect(rows().at(-1)).toEqual({ name: "Zeta Agent", tag: MEMORY_TOOL_TAG });
  unmount();
  setRoster([...ROSTER, eighth({ tools: { mcp: null } })]);
  render(<AgentAccessCard enabled paragraph={PARAGRAPH} onCopy={() => {}} />);
  expect(rows().at(-1)).toEqual({ name: "Zeta Agent", tag: PARAGRAPH_TAG });
  const old = { ...eighth({}) } as Partial<EngineInfo>;
  delete old.tools;
  expect(getsMemoryTool(old as EngineInfo)).toBe(false);
});

test("the paragraph waits behind a disclosure for agents without the tool, and copies", async () => {
  const onCopy = vi.fn();
  render(<AgentAccessCard enabled paragraph={PARAGRAPH} onCopy={onCopy} />);
  const card = screen.getByRole("region", { name: "Agent access" });
  const paragraph = within(card).getByText(PARAGRAPH);
  expect(paragraph).not.toBeVisible(); // collapsed by default
  await userEvent.click(within(card).getByText("For agents without the tool"));
  expect(paragraph).toBeVisible();
  // The paste line names the instruction files of the agents WITHOUT the tool, from the roster.
  const files = new Set(
    memoryAgents(ROSTER).filter((e) => !getsMemoryTool(e)).flatMap((e) => e.instructions?.files ?? []),
  );
  for (const f of files) expect(card).toHaveTextContent(f);
  await userEvent.click(within(card).getByRole("button", { name: "Copy paragraph" }));
  expect(onCopy).toHaveBeenCalledTimes(1);
});

test("memory off says new sessions get nothing and running ones keep access — no list, no paragraph", () => {
  render(<AgentAccessCard enabled={false} paragraph={PARAGRAPH} onCopy={() => {}} />);
  const card = screen.getByRole("region", { name: "Agent access" });
  expect(card).toHaveTextContent(OFF_TEXT);
  expect(OFF_TEXT).toBe(
    "Memory is off — new sessions get nothing. Sessions already running keep access until they restart or stop.",
  );
  expect(within(card).queryByRole("list")).toBeNull();
  expect(within(card).queryByText(PARAGRAPH)).toBeNull();
  expect(within(card).getByRole("link", { name: "Which agents can reach it" })).toBeVisible();
});

test("before the roster loads nothing is guessed", () => {
  resetRoster();
  render(<AgentAccessCard enabled paragraph={PARAGRAPH} onCopy={() => {}} />);
  expect(screen.getByRole("region", { name: "Agent access" })).toHaveTextContent("Loading agents…");
  expect(screen.queryByRole("list")).toBeNull();
});
