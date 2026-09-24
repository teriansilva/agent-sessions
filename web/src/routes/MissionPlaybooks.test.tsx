/** The playbook editor (#892).
 *
 * What is worth pinning here is not that inputs change state — it is the two places this panel
 * touches the authority model of #883:
 *
 *  - a probe's ARGUMENTS belong to its KIND, so changing the kind must not carry the old ones
 *    over into a shape the server will refuse;
 *  - the server's refusal is the operator's only instruction for fixing a bad template, so it is
 *    surfaced verbatim rather than paraphrased into "something went wrong".
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx, ConfigRefreshCtx } from "../app/config";
import { ApiError, api } from "../lib/api";
import type { AppConfig, MissionPlaybooks as Block } from "../types/api";

import { MissionPlaybooks } from "./MissionPlaybooks";

vi.mock("../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

// The server's real contract since #1088: `supervisor_judged` exists and NOTHING is non-gating.
const SCHEMA = {
  kinds: ["none", "supervisor_judged", "forge_pr", "http_status"],
  non_gating: [] as string[],
  args: {
    none: { required: [], optional: [] },
    supervisor_judged: { required: [], optional: [] },
    forge_pr: { required: [], optional: ["branch", "repo"] },
    http_status: { required: ["url"], optional: ["expect_status"] },
  },
  // The JSON TYPE of each argument, as the server publishes it from `PROBE_ARG_TYPES`.
  types: {
    none: {},
    supervisor_judged: {},
    forge_pr: { branch: "text", repo: "text" },
    http_status: { url: "text", expect_status: "int" },
  },
};

function block(): Block {
  return {
    default_id: "ship",
    playbooks: [
      {
        id: "ship",
        label: "Ship it",
        objectives: [
          {
            key: "live",
            title: "It is live",
            probe: "http_status",
            probe_args: { url: "https://example.test/healthz" },
            gate: true,
          },
        ],
      },
    ],
  };
}

function renderPanel(
  b: Block = block(),
  refresh: () => void = () => {},
  schema: typeof SCHEMA = SCHEMA,
) {
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    mission_playbooks: b,
    mission_probes: schema,
  } as AppConfig;
  return render(
    <ConfigRefreshCtx.Provider value={refresh}>
      <ConfigCtx.Provider value={config}>
        <MissionPlaybooks />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );
}

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset().mockResolvedValue({});
});

test("the stored playbooks are what the editor opens on", () => {
  renderPanel();
  expect(screen.getByTestId("playbook-label")).toHaveValue("Ship it");
  expect(screen.getByTestId("objective-arg-url")).toHaveValue(
    "https://example.test/healthz",
  );
  expect(screen.getByTestId("playbook-default")).toHaveValue("ship");
});

test("changing the probe KIND drops the arguments that belonged to the old one", async () => {
  // `probe_args` is validated per kind and unknown keys are REJECTED, not ignored (#883). So a
  // `url` carried across from `http_status` to `forge_pr` would make every later save fail with
  // "forge_pr does not take url" — an error about a field the operator can no longer see.
  renderPanel();
  await userEvent.selectOptions(
    screen.getByTestId("objective-probe"),
    "forge_pr",
  );
  expect(screen.queryByTestId("objective-arg-url")).toBeNull();

  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  const obj = sent.mission_playbooks.playbooks[0].objectives[0];
  expect(obj.probe).toBe("forge_pr");
  expect(obj.probe_args).toBeNull();
});

test("clearing an argument REMOVES it rather than sending an empty string", async () => {
  // The server refuses a blank where it wants text, so an emptied field that still rode along
  // would be a save the operator cannot complete without knowing to retype something.
  renderPanel();
  await userEvent.clear(screen.getByTestId("objective-arg-url"));
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  expect(
    sent.mission_playbooks.playbooks[0].objectives[0].probe_args,
  ).toBeNull();
});

test("removing the DEFAULT playbook clears the default with it", async () => {
  // `default_id` naming no playbook is a refusal, so leaving it behind would make the very next
  // save fail for a reason nothing on screen explains.
  renderPanel();
  await userEvent.click(screen.getByTestId("playbook-remove"));
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  expect(sent.mission_playbooks.default_id).toBe("");
  expect(sent.mission_playbooks.playbooks).toEqual([]);
});

test("the server's REFUSAL is shown as it was written", async () => {
  // It names the id, the key and the reason. A friendlier sentence would be a less useful one:
  // the whole value of the message is that it says which row to fix.
  vi.mocked(api.setPrefs).mockRejectedValue(
    new ApiError(422, "bad objective key 'Checks Green'"),
  );
  renderPanel();
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() =>
    expect(screen.getByTestId("playbook-error")).toHaveTextContent(
      "bad objective key 'Checks Green'",
    ),
  );
});

test("a non-gating probe kind cannot be made a gate (the mechanism, kept for a future kind)", async () => {
  // "The agent believes it wrote tests" is not evidence that it did, so the control is withdrawn
  // rather than offered and then refused on save.
  renderPanel({
    default_id: "",
    playbooks: [
      {
        id: "p",
        label: "P",
        objectives: [
          {
            key: "k",
            title: "T",
            probe: "supervisor_judged",
            probe_args: null,
            gate: false,
          },
        ],
      },
    ],
  }, () => {}, { ...SCHEMA, non_gating: ["supervisor_judged"] });
  expect(screen.getByTestId("objective-gate")).toBeDisabled();
});

test("'Supervisor judges' is offered by name and CAN gate (#1088)", () => {
  renderPanel({
    default_id: "",
    playbooks: [
      {
        id: "p",
        label: "P",
        objectives: [
          { key: "k", title: "T", probe: "supervisor_judged", probe_args: null, gate: true },
        ],
      },
    ],
  });
  const pick = screen.getByTestId("objective-probe") as HTMLSelectElement;
  expect(pick.value).toBe("supervisor_judged");
  expect(pick.selectedOptions[0].text).toBe("Supervisor judges");
  expect(screen.getByTestId("objective-gate")).toBeEnabled();
  expect(screen.getByTestId("objective-gate")).toBeChecked();
});

test("an INTEGER argument is sent as a number, not as the string the input yields", async () => {
  // `http_status.expect_status` is strictly `int` server-side (`_arg_status` refuses a `str`,
  // and refuses `bool` before that). Every HTML input yields a string, so before the schema
  // carried types this was a field the editor offered and the server could only ever refuse —
  // the operator typed `204`, saw "expect_status must be an integer", and had no way to comply
  // (#900 review, finding 6).
  renderPanel();
  await userEvent.type(
    screen.getByTestId("objective-arg-expect_status"),
    "204",
  );
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  const args = sent.mission_playbooks.playbooks[0].objectives[0].probe_args!;
  expect(args.expect_status).toBe(204);
  expect(typeof args.expect_status).toBe("number");
  // ...and a text argument is still a string — the typing is per argument, not per panel.
  expect(typeof args.url).toBe("string");
});

test("a NON-INTEGER typed into an int field reaches the server, rather than becoming NaN", async () => {
  // Coercing it would send `null`/`NaN` and the refusal would name nothing the operator can see;
  // dropping it would make the field appear to accept a value it silently discarded. The raw
  // string travels and the server's own message says what is wrong with it.
  renderPanel();
  await userEvent.type(
    screen.getByTestId("objective-arg-expect_status"),
    "2xx",
  );
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  expect(
    sent.mission_playbooks.playbooks[0].objectives[0].probe_args!.expect_status,
  ).toBe("2xx");
});

test("an edit made WHILE a save is in flight survives the response", async () => {
  // The response replaces the whole block with the server's normalized copy, and the config
  // refresh that follows re-seeds it a second time. Both are round trips; typing is not. Either
  // one landing on top of a newer draft discards work the operator watched themselves do (#900
  // review, finding 7).
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.setPrefs>,
  );

  // The refresh call is the marker that the RESPONSE has been handled — it runs after the block
  // is (or is not) replaced. Asserting the label straight after `release` would pass before the
  // response landed at all, which is a green that proves nothing.
  const refresh = vi.fn();
  renderPanel(block(), refresh);
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());

  // ...the operator keeps working while it is in flight.
  await userEvent.clear(screen.getByTestId("playbook-label"));
  await userEvent.type(screen.getByTestId("playbook-label"), "Ship it twice");

  // ...and the server answers about the block that was SENT.
  release({ mission_playbooks: block() });
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  expect(screen.getByTestId("playbook-label")).toHaveValue("Ship it twice");
});

test("a config refresh does NOT overwrite an unsaved draft", async () => {
  // The other half of the same rule: `refreshConfig()` publishes a new config object, the panel
  // re-seeds from it, and an unfenced re-seed throws the draft away just as thoroughly as the
  // save response does.
  const { rerender } = renderPanel();
  await userEvent.clear(screen.getByTestId("playbook-label"));
  await userEvent.type(screen.getByTestId("playbook-label"), "Local draft");

  // A NEW object with the same content — which is exactly what a refresh produces.
  const config = {
    csrf: "t",
    new_session_engines: [],
    terminal_backend: "ws",
    mission_playbooks: block(),
    mission_probes: SCHEMA,
  } as AppConfig;
  rerender(
    <ConfigRefreshCtx.Provider value={() => {}}>
      <ConfigCtx.Provider value={config}>
        <MissionPlaybooks />
      </ConfigCtx.Provider>
    </ConfigRefreshCtx.Provider>,
  );

  expect(screen.getByTestId("playbook-label")).toHaveValue("Local draft");
});

test("a DUPLICATE id can be corrected, not locked in", async () => {
  // "New" used to be inferred by comparing the draft id to the stored ids, so typing an id that
  // already existed made the field immutable `<code>` — Save then refused the duplicate and the
  // only way out was to delete the whole row and start again (#900 review 2, finding 4).
  renderPanel();
  await userEvent.click(screen.getByTestId("playbook-add"));
  const ids = screen.getAllByTestId("playbook-id");
  const fresh = ids[ids.length - 1];

  // Typing the EXISTING id must not turn the input into static text…
  await userEvent.type(fresh, "ship");
  expect(screen.getAllByTestId("playbook-id")).toHaveLength(ids.length);
  expect(fresh).toHaveValue("ship");

  // …and it is still correctable in place.
  await userEvent.clear(fresh);
  await userEvent.type(fresh, "ship-two");
  expect(fresh).toHaveValue("ship-two");

  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  const sent = vi.mocked(api.setPrefs).mock.calls[0][0] as {
    mission_playbooks: Block;
  };
  expect(sent.mission_playbooks.playbooks.map((p) => p.id)).toEqual([
    "ship",
    "ship-two",
  ]);
});

test("a STORED playbook's id stays immutable", async () => {
  // The mirror, and the reason provenance is the right test: `playbook_id` is what a mission
  // stores, so renaming one after it has been used would orphan every mission naming it.
  renderPanel();
  expect(screen.queryByTestId("playbook-id")).toBeNull();
  expect(screen.getByText("ship")).toBeInTheDocument();
});

test("REMOVING a row keeps the right rows editable", async () => {
  // Draft-ness is tracked by index, and a removal shifts every index after it. Without the
  // remap the wrong row becomes editable — or an editable one silently freezes.
  renderPanel();
  await userEvent.click(screen.getByTestId("playbook-add"));
  await userEvent.click(screen.getByTestId("playbook-add"));
  expect(screen.getAllByTestId("playbook-id")).toHaveLength(2);

  // Remove the FIRST draft (the second row overall; index 1).
  const removes = screen.getAllByTestId("playbook-remove");
  await userEvent.click(removes[1]);

  // One draft left, and it is still editable.
  const left = screen.getAllByTestId("playbook-id");
  expect(left).toHaveLength(1);
  await userEvent.type(left[0], "later");
  expect(left[0]).toHaveValue("later");
});

test("a SUCCESSFUL save settles provenance even if the operator kept typing", async () => {
  // The server ACCEPTED the id, so it is an identity missions can name from that moment —
  // whether or not a newer edit is on screen. Keeping the provenance update inside the revision
  // fence left a persisted id editable, and it could then be renamed out from under a mission
  // that already referenced it (#900 review 3, finding 3).
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.setPrefs>,
  );

  const refresh = vi.fn();
  renderPanel(block(), refresh);
  await userEvent.click(screen.getByTestId("playbook-add"));
  const ids = screen.getAllByTestId("playbook-id");
  await userEvent.type(ids[ids.length - 1], "foo");
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());

  // …the operator keeps typing while it is in flight.
  const labels = screen.getAllByTestId("playbook-label");
  await userEvent.type(labels[labels.length - 1], "Later");

  // …and the server accepts the block that was sent.
  release({
    mission_playbooks: {
      default_id: "ship",
      playbooks: [
        ...block().playbooks,
        { id: "foo", label: "", objectives: [] },
      ],
    },
  });
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  // THE NEWER EDIT SURVIVES…
  expect(screen.getAllByTestId("playbook-label").at(-1)).toHaveValue("Later");
  // …and the id is no longer editable, because the server has it.
  expect(screen.queryByTestId("playbook-id")).toBeNull();
});

test("a row ADDED during a save is not frozen by that save's response", async () => {
  // #900 review 4, finding 3, and the sibling defect the previous fix introduced. Clearing the
  // WHOLE draft set on any success settles provenance for rows the request never carried: save A
  // is pending, the operator adds `bar`, A resolves with a snapshot containing only `foo`, and
  // `bar`'s id input turns into immutable `<code>` — an identity missions can name that the
  // server has never seen, and that the operator can no longer correct.
  //
  // Red against a provenance update keyed on anything but the SUBMITTED rows.
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.setPrefs>,
  );

  const refresh = vi.fn();
  renderPanel(block(), refresh);
  await userEvent.click(screen.getByTestId("playbook-add"));
  const first = screen.getAllByTestId("playbook-id");
  await userEvent.type(first[first.length - 1], "foo");
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());

  // A SECOND ROW, added while A is in flight. It was never sent.
  await userEvent.click(screen.getByTestId("playbook-add"));
  const both = screen.getAllByTestId("playbook-id");
  expect(both).toHaveLength(2);
  await userEvent.type(both[both.length - 1], "bar");

  release({
    mission_playbooks: {
      default_id: "ship",
      playbooks: [
        ...block().playbooks,
        { id: "foo", label: "", objectives: [] },
      ],
    },
  });
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  // `foo` was accepted, so its id is settled — exactly one editable id is left, and it is `bar`.
  const left = screen.getAllByTestId("playbook-id");
  expect(left).toHaveLength(1);
  expect(left[0]).toHaveValue("bar");
});

test("REMOVING a row does not move another row's provenance onto it", async () => {
  // The set was keyed on INDEX and remapped on removal, which is the same class of bug one level
  // down: a position is not an identity, and the remap only happens to be right when nothing else
  // is in flight. Keyed on the row, removal touches nothing but the row removed.
  vi.mocked(api.setPrefs).mockResolvedValue({} as never);
  renderPanel(block());
  await userEvent.click(screen.getByTestId("playbook-add"));
  await userEvent.click(screen.getByTestId("playbook-add"));
  const ids = screen.getAllByTestId("playbook-id");
  expect(ids).toHaveLength(2);
  await userEvent.type(ids[0], "aaa");
  await userEvent.type(ids[1], "bbb");

  // Remove the FIRST draft. The stored row is index 0, so the drafts are 1 and 2.
  const removes = screen.getAllByTestId("playbook-remove");
  await userEvent.click(removes[1]);

  const left = screen.getAllByTestId("playbook-id");
  expect(left).toHaveLength(1);
  expect(left[0]).toHaveValue("bbb");
});

test("a row RENAMED during a save keeps its id editable", async () => {
  // #900 review 7, finding 7. Provenance was settled on the stable ROW id alone, and the row is
  // not the identity the server accepted — the playbook id is. Submit `foo`, rename it to `bar`
  // while the request is in flight, and the success froze `bar` as immutable although only `foo`
  // exists server-side: an identity missions can name that has never been saved, and one the
  // operator can no longer correct.
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.setPrefs>,
  );

  const refresh = vi.fn();
  renderPanel(block(), refresh);
  await userEvent.click(screen.getByTestId("playbook-add"));
  const ids = screen.getAllByTestId("playbook-id");
  await userEvent.type(ids[ids.length - 1], "foo");
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());

  // …renamed while the request is in flight.
  const pending = screen.getAllByTestId("playbook-id");
  await userEvent.clear(pending[pending.length - 1]);
  await userEvent.type(pending[pending.length - 1], "bar");

  release({
    mission_playbooks: {
      revision: 1,
      default_id: "ship",
      playbooks: [
        ...block().playbooks,
        { id: "foo", label: "", objectives: [] },
      ],
    },
  });
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  // STILL EDITABLE: `bar` is a name the server has never seen.
  const after = screen.getAllByTestId("playbook-id");
  expect(after.at(-1)).toHaveValue("bar");
});

test("a save that lands under a newer draft still carries the revision forward", async () => {
  // #900 review 7, finding 6. The save SUCCEEDED — the server is at N+1 — and keeping the
  // on-screen block at N meant the operator's next save was refused with a 409 whose only
  // offered remedy is a reload, which throws away the very edit the fence was protecting.
  let release: (v: unknown) => void = () => {};
  vi.mocked(api.setPrefs).mockImplementation(
    () =>
      new Promise((res) => {
        release = res;
      }) as ReturnType<typeof api.setPrefs>,
  );

  const refresh = vi.fn();
  renderPanel({ ...block(), revision: 3 } as Block, refresh);
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalled());
  expect(
    (vi.mocked(api.setPrefs).mock.calls[0][0] as { mission_playbooks: Block })
      .mission_playbooks.revision,
  ).toBe(3);

  // …the operator keeps typing, so the block on screen is newer than the one that was sent.
  await userEvent.type(screen.getByTestId("playbook-label"), " more");
  release({ mission_playbooks: { ...block(), revision: 4 } });
  await waitFor(() => expect(refresh).toHaveBeenCalled());

  // THE DRAFT SURVIVES …
  expect(screen.getByTestId("playbook-label")).toHaveValue("Ship it more");
  // … and the next save names the revision the server actually accepted, so it is not a 409.
  vi.mocked(api.setPrefs).mockResolvedValue({});
  await userEvent.click(screen.getByTestId("playbook-save"));
  await waitFor(() => expect(api.setPrefs).toHaveBeenCalledTimes(2));
  expect(
    (vi.mocked(api.setPrefs).mock.calls[1][0] as { mission_playbooks: Block })
      .mission_playbooks.revision,
  ).toBe(4);
});

test("removing a DUPLICATE draft does not unset the real default", async () => {
  // #900 review 7, finding 10. The default was cleared by comparing the removed row's id against
  // `default_id` — so removing a DRAFT the operator had also typed `ship` into unset the role of
  // the stored `ship`, which is still right there in the list.
  renderPanel();
  expect(screen.getByTestId("playbook-default")).toHaveValue("ship");

  await userEvent.click(screen.getByTestId("playbook-add"));
  const ids = screen.getAllByTestId("playbook-id");
  await userEvent.type(ids[ids.length - 1], "ship");

  // Remove the DRAFT — the last row's own remove button.
  const removes = screen.getAllByTestId("playbook-remove");
  await userEvent.click(removes[removes.length - 1]);

  // The real `ship` is still there, and still the default.
  expect(screen.getByTestId("playbook-default")).toHaveValue("ship");
});
