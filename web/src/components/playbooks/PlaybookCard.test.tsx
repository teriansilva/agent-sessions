import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import {
  markRosterFailed,
  resetRoster,
  setRoster,
} from "../../app/engineRoster";
import fixture from "../../test/roster.fixture.json";
import type { EngineInfo } from "../../types/api";
import type { PlaybookStep } from "../../types/playbooks";
import { PlaybookActor } from "./PlaybookCard";

afterEach(() => {
  cleanup();
  resetRoster();
});
const engine: EngineInfo = {
  ...(fixture.engines[0] as EngineInfo),
  id: "fixture-agent",
  present: true,
  status: "active",
  model_select: {
    supported: true,
    on_resume: true,
    configured_elsewhere: false,
    offered: [
      {
        id: "careful-model",
        aliases: ["careful"],
        context_window: null,
        source: "operator",
      },
    ],
  },
};
const step: PlaybookStep = {
  id: "review",
  title: "Review",
  after: [],
  note: false,
  actor: { kind: "agent", engine: engine.id, model: "careful" },
};

test.each([
  [[], "Unresolved: agent absent"],
  [[{ ...engine, status: "retiring" }], "Unresolved: agent retiring"],
  [
    [{ ...engine, model_select: { ...engine.model_select!, offered: [] } }],
    "Unresolved: model unavailable",
  ],
] as const)(
  "unavailable references remain visible and unchanged: %s",
  (rows, reason) => {
    const original = JSON.stringify(step);
    setRoster(rows);
    render(<PlaybookActor step={step} />);
    expect(screen.getByText(reason)).toBeVisible();
    expect(screen.getByText("careful")).toBeVisible();
    expect(JSON.stringify(step)).toBe(original);
  },
);

test("an offered alias resolves for display; a failed roster refresh makes availability unknown", () => {
  setRoster([engine]);
  render(<PlaybookActor step={step} />);
  expect(screen.queryByText(/Unresolved/)).not.toBeInTheDocument();
  act(() => markRosterFailed());
  expect(screen.getByText("Roster unavailable")).toBeVisible();
  expect(screen.getByText("careful")).toBeVisible();
});
