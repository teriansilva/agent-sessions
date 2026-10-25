/** A new mission has no checklist (#1653: checklists were removed), asserted on the REQUEST.
 *
 *  - There is no picker, even when a config still carries an old `mission_playbooks` block.
 *  - No `playbook_id` is sent.
 *  - The line under the box says how the mission is measured: AI-written, supervisor-judged
 *    objectives with an endpoint, none written without one.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../../app/config";
import { api } from "../../lib/api";
import type { AppConfig } from "../../types/api";

import { NewMissionForm } from "./NewMissionForm";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { projectEntities: vi.fn(), createMission: vi.fn(), folders: vi.fn() },
  };
});

/** A config as an OLD server (or a stale cache) might still send it: a checklist block, and an
 *  AI endpoint either configured or not. */
function cfg(aiConfigured: boolean) {
  return {
    mission_playbooks: {
      default_id: "pr",
      playbooks: [{ id: "pr", label: "Ship a PR", objectives: [] }],
      revision: 1,
    },
    ai_review: { configured: aiConfigured },
  } as unknown as AppConfig;
}

function form(config: AppConfig | null) {
  return (
    <ConfigCtx.Provider value={config}>
      <NewMissionForm
        visit={() => 0}
        isVisitCurrent={() => true}
        onCreated={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
}

async function start() {
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  await userEvent.type(
    screen.getByTestId("new-mission-instruction"),
    "do the thing",
  );
  await userEvent.selectOptions(
    await screen.findByTestId("new-mission-project"),
    "p1",
  );
  await userEvent.click(screen.getByTestId("new-mission-start"));
  await waitFor(() => expect(api.createMission).toHaveBeenCalled());
  return vi.mocked(api.createMission).mock.calls[0][0];
}

beforeEach(() => {
  vi.mocked(api.projectEntities)
    .mockReset()
    .mockResolvedValue({
      projects: [{ id: "p1", name: "battlelab" }],
    } as never);
  vi.mocked(api.createMission)
    .mockReset()
    .mockResolvedValue({ id: "msn_9" } as never);
});

test("there is no checklist picker and no playbook_id is sent", async () => {
  render(form(cfg(true)));
  expect(screen.queryByTestId("new-mission-playbook")).toBeNull();
  expect(screen.queryByRole("combobox", { name: "Checklist" })).toBeNull();
  const body = await start();
  expect(body).not.toHaveProperty("playbook_id");
  expect(body).toEqual({ instruction: "do the thing", project_id: "p1" });
});

test("with an AI endpoint, the line under the box says the orchestrator writes the objectives", () => {
  render(form(cfg(true)));
  const note = screen.getByTestId("new-mission-objectives-note");
  expect(note.textContent).toMatch(/orchestrator writes the objectives/);
  expect(note.textContent).not.toMatch(/checklist/i);
});

test("without an endpoint, the line under the box says no objectives are written", () => {
  render(form(cfg(false)));
  const note = screen.getByTestId("new-mission-objectives-note");
  expect(note.textContent).toMatch(/no objectives are written for you/);
  expect(note.textContent).not.toMatch(/checklist/i);
});
