/** Which checklist a new mission gets (#1061 Phase 1), asserted on the REQUEST.
 *
 *  - The configured default is pre-selected, and follows a config that lands after mount.
 *  - The choice is sent explicitly — including "No checklist" (`:none`) — so the mission records
 *    what the operator saw selected, not whatever the Settings default becomes later.
 *  - With no playbooks configured there is no picker and no `playbook_id`: the old request.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../../app/config";
import { api } from "../../lib/api";
import type { AppConfig } from "../../types/api";

import { NewMissionForm, PLAYBOOK_DECLINED } from "./NewMissionForm";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: { projectEntities: vi.fn(), createMission: vi.fn(), folders: vi.fn() },
  };
});

const pb = (id: string, label: string) => ({ id, label, objectives: [] });

function cfg(
  default_id: string,
  playbooks = [pb("pr", "Ship a PR"), pb("audit", "Audit")],
) {
  return {
    mission_playbooks: { default_id, playbooks, revision: 1 },
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

test("the configured default is pre-selected and sent explicitly", async () => {
  render(form(cfg("audit")));
  const pick = screen.getByTestId("new-mission-playbook") as HTMLSelectElement;
  expect(pick.value).toBe("audit");
  expect([...pick.options].map((o) => o.text)).toEqual([
    "Checklist: Ship a PR",
    "Checklist: Audit",
    "No checklist",
  ]);
  expect((await start()).playbook_id).toBe("audit");
});

test("declining sends :none", async () => {
  render(form(cfg("pr")));
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-playbook"),
    PLAYBOOK_DECLINED,
  );
  expect((await start()).playbook_id).toBe(":none");
});

test("with no default configured, 'No checklist' is what is selected — and what is sent", async () => {
  render(form(cfg("")));
  expect(
    (screen.getByTestId("new-mission-playbook") as HTMLSelectElement).value,
  ).toBe(":none");
  expect((await start()).playbook_id).toBe(":none");
});

test("a config that lands after mount moves an UNTOUCHED pre-selection, never a pick", async () => {
  const { rerender } = render(form(null));
  expect(screen.queryByTestId("new-mission-playbook")).toBeNull();
  rerender(form(cfg("pr")));
  const pick = screen.getByTestId("new-mission-playbook") as HTMLSelectElement;
  expect(pick.value).toBe("pr");
  await userEvent.selectOptions(pick, "audit");
  rerender(form(cfg("pr")));
  expect(
    (screen.getByTestId("new-mission-playbook") as HTMLSelectElement).value,
  ).toBe("audit");
});

test("a picked playbook that vanishes from the config falls back to the default", async () => {
  const { rerender } = render(form(cfg("pr")));
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-playbook"),
    "audit",
  );
  rerender(form(cfg("pr", [pb("pr", "Ship a PR")])));
  expect(
    (screen.getByTestId("new-mission-playbook") as HTMLSelectElement).value,
  ).toBe("pr");
  expect((await start()).playbook_id).toBe("pr");
});

test("no playbooks configured: no picker, and no playbook_id on the request", async () => {
  render(form(cfg("", [])));
  expect(screen.queryByTestId("new-mission-playbook")).toBeNull();
  expect("playbook_id" in (await start())).toBe(false);
});

test("the line under the box says what the checklist is — and what declining costs", async () => {
  const withObjectives = {
    mission_playbooks: {
      default_id: "pr",
      revision: 1,
      playbooks: [
        {
          id: "pr",
          label: "Ship a PR",
          objectives: [
            {
              key: "open",
              title: "PR open",
              probe: "forge_pr_open",
              probe_args: null,
              gate: true,
            },
            {
              key: "merged",
              title: "merged",
              probe: "forge_merged",
              probe_args: null,
              gate: true,
            },
          ],
        },
      ],
    },
  } as unknown as AppConfig;
  render(form(withObjectives));
  const note = () => screen.getByTestId("new-mission-playbook-note");
  expect(note().textContent).toBe(
    "Ship a PR — PR open · merged. Checked by the server; fitted to your instruction.",
  );
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-playbook"),
    PLAYBOOK_DECLINED,
  );
  expect(note().textContent).toMatch(
    /^No checklist — notes only: .*never confirm itself finished/,
  );
});

test("no playbooks configured: no line under the box either", () => {
  render(form(cfg("", [])));
  expect(screen.queryByTestId("new-mission-playbook-note")).toBeNull();
});

test("with an AI endpoint, 'No checklist' means the orchestrator writes the objectives (#1088)", async () => {
  const c = {
    ...cfg("pr"),
    ai_review: { configured: true },
    orchestrator: { judge_confidence_min: 0.93 },
  } as unknown as AppConfig;
  render(form(c));
  const pick = screen.getByTestId("new-mission-playbook") as HTMLSelectElement;
  // The id is still `:none`; only what the operator reads changes.
  const declined = [...pick.options].find((o) => o.value === PLAYBOOK_DECLINED)!;
  expect(declined.text).toBe("No checklist — AI writes the objectives");
  await userEvent.selectOptions(pick, PLAYBOOK_DECLINED);
  expect(screen.getByTestId("new-mission-playbook-note").textContent).toBe(
    "No checklist — the orchestrator writes the objectives from your instruction, and the " +
      "supervisor judges them. When it is at least 0.93 sure the work is done, the mission " +
      "moves to review. You close it.",
  );
  expect((await start()).playbook_id).toBe(":none");
});
