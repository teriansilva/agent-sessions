/** The mission rail's origin badge (#1201, #1252 review): the server records a mission's origin
 *  under `mission:<id>`, and the rail must look it up under that key, not the bare id. */
import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { MissionListRow } from "../../types/api";
import { resetOriginsForTest } from "../../app/automationOrigins";
import { MissionRail } from "./MissionRail";

afterEach(() => {
  vi.restoreAllMocks();
  resetOriginsForTest();
});

test("an automated mission wears its badge in the rail, found by its qualified key", async () => {
  // Exactly the server's shape (`automations_store.origins`): keyed `mission:<id>`.
  vi.spyOn(api, "automationOrigins").mockResolvedValue({
    origins: {
      "mission:msn_1": {
        kind: "mission",
        automation_id: "a1",
        name: "Nightly dependency audit",
        run_id: "r1",
        deleted: false,
      },
    },
  });
  render(
    <MissionRail
      missions={[
        {
          id: "msn_1",
          title: "Dependency audit",
          project_id: null,
          cwd: null,
          state: "running",
          created_at: 1,
          updated_at: 1,
          closed_at: null,
          archived_at: null,
          outcome: null,
          session_keys: [],
        },
      ]}
      selectedId={null}
      onSelect={() => {}}
      hasMore={false}
    />,
  );
  expect(await screen.findByTestId("origin-badge")).toHaveTextContent("Auto · Nightly dependency audit");
});

// ---- arrivals vs pages (#1573, Hermes 6757) ------------------------------------------------------

const row = (n: number): MissionListRow =>
  ({
    id: `msn_${n}`,
    title: `Mission ${n}`,
    project_id: null,
    cwd: null,
    state: "running",
    created_at: n,
    updated_at: n,
    closed_at: null,
    archived_at: null,
    outcome: null,
    session_keys: [],
  }) as MissionListRow;

function rail(missions: MissionListRow[], hasMore: boolean) {
  return <MissionRail missions={missions} selectedId={null} onSelect={() => {}} hasMore={hasMore} />;
}
const arrived = () =>
  screen.queryAllByTestId("rail-mission").filter((el) => /rowArrived/.test(el.className)).map((el) => el.textContent);

test("a short LOAD MORE page is more of the list, not new work: nothing arrives", () => {
  vi.spyOn(api, "automationOrigins").mockResolvedValue({ origins: {} });
  document.documentElement.dataset.motion = "full";
  const page1 = [1, 2, 3, 4, 5].map(row);
  const { rerender } = render(rail(page1, true));
  // The last page: two missions that already existed, appended after the first page.
  rerender(rail([...page1, row(6), row(7)], false));
  expect(arrived()).toEqual([]);
  delete document.documentElement.dataset.motion;
});

test("a mission that joins at the head while more pages exist still arrives", () => {
  vi.spyOn(api, "automationOrigins").mockResolvedValue({ origins: {} });
  document.documentElement.dataset.motion = "full";
  const page1 = [1, 2, 3, 4, 5].map(row);
  const { rerender } = render(rail(page1, true));
  rerender(rail([row(9), ...page1], true));
  expect(arrived()).toHaveLength(1);
  expect(arrived()[0]).toContain("Mission 9");
  delete document.documentElement.dataset.motion;
});
