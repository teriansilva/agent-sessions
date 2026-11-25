import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { resetRoster, setRoster } from "../../app/engineRoster";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import { memoryAgents } from "../memory/agentAccess";
import { HELPER_TAG, SecretChannels, SECRETS_TOOL_TAG } from "./SecretChannels";

const ROSTER = fixture.engines as unknown as EngineInfo[];

afterEach(() => resetRoster());

test("each agent's channel comes from its manifest: the secrets tool where it declares one, else the helper", () => {
  const base = ROSTER.find((e) => e.kind === "agent" && e.runtime === "pty")!;
  const zeta = { ...base, id: "zeta", label: "Zeta Agent", tools: {}, display: { ...base.display, name: "zeta", badge: "zt", order: 999 } } as EngineInfo;
  setRoster([...ROSTER, zeta]);
  render(<SecretChannels />);
  const list = screen.getByRole("list", { name: "How each agent reaches secrets" });
  const rows = within(list).getAllByRole("listitem").map((li) => [li.firstChild?.textContent, li.lastChild?.textContent]);
  expect(rows).toEqual(memoryAgents([...ROSTER, zeta]).map((e) => [e.label, e.tools?.mcp ? SECRETS_TOOL_TAG : HELPER_TAG]));
  expect(rows).toContainEqual(["Zeta Agent", HELPER_TAG]);
  expect(rows.some(([, tag]) => tag === SECRETS_TOOL_TAG)).toBe(true);
});

test("nothing is shown before the roster loads", () => {
  resetRoster();
  const { container } = render(<SecretChannels />);
  expect(container).toBeEmptyDOMElement();
});
