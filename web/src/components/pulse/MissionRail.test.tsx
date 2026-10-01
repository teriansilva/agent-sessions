/** The mission rail's origin badge (#1201, #1252 review): the server records a mission's origin
 *  under `mission:<id>`, and the rail must look it up under that key, not the bare id. */
import { render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
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
