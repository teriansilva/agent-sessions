import * as React from "react";
/** NEW MISSION — the composer mode that makes the console able to start anything (#889).
 *
 *  Two properties matter here and both are asserted on the REQUEST, not on the rendering:
 *
 *  1. **`cwd` is never sent.** The route refuses it outright (422) and resolves the path itself
 *     from `project_id`. A client that sent one would be handing itself an authority the design
 *     deliberately keeps server-side.
 *  2. **The project is an ENTITY id, not a folder cwd.** `GET /api/folders` and
 *     `GET /api/projects` are different lists and the route resolves against the second; sending
 *     a folder row's id (which IS its cwd) is a 404 "unknown project". The two pickers look
 *     interchangeable and are not, so the call itself is pinned.
 */
import { useEffect, useRef, useState } from "react";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../../lib/api";

import { Composer } from "./Composer";

/** `Composer` no longer owns its own mode: the console does, so that "+ New mission" in the
 *  sidebar can switch a composer that has not mounted yet (#937 review 1, finding 2). These
 *  tests therefore supply the same controlled pair the console does, rather than asserting on
 *  state the component stopped holding. Behaviour under test is unchanged — the mode buttons
 *  still drive it, through the callback instead of through `useState`. */
function ControlledComposer(
  props: Omit<
    React.ComponentProps<typeof Composer>,
    "creating" | "onCreatingChange"
  >,
) {
  const [creating, setCreating] = React.useState(false);
  return (
    <Composer {...props} creating={creating} onCreatingChange={setCreating} />
  );
}

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return {
    ...actual,
    api: {
      pulseAsk: vi.fn(),
      projectEntities: vi.fn(),
      createMission: vi.fn(),
      // Present so that a component reaching for the WRONG picker is a visible call rather than
      // a crash that could be mistaken for something else.
      folders: vi.fn(),
    },
  };
});

function mount(onCreated = vi.fn()) {
  render(
    <ControlledComposer
      missionId="untracked"
      configured
      turns={[]}
      onTurns={() => {}}
      visit={() => 0}
      isVisitCurrent={() => true}
      onCreated={onCreated}
    />,
  );
  return onCreated;
}

beforeEach(() => {
  vi.mocked(api.projectEntities)
    .mockReset()
    .mockResolvedValue({
      projects: [
        { id: "p1", name: "battlelab" },
        { id: "p2", name: "infra" },
      ] as never,
    });
  vi.mocked(api.createMission).mockReset();
  vi.mocked(api.folders).mockReset();
});

test("the mode is reachable with NO AI endpoint — creating never needed a model", async () => {
  render(
    <ControlledComposer
      missionId="untracked"
      configured={false}
      turns={[]}
      onTurns={() => {}}
      visit={() => 0}
      isVisitCurrent={() => true}
      onCreated={vi.fn()}
    />,
  );
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  expect(screen.getByTestId("new-mission-form")).toBeInTheDocument();
  // The ASK half genuinely needs one and stays disabled; this half does not.
  expect(screen.getByTestId("new-mission-instruction")).toBeEnabled();
});

test("it posts the instruction and a project ENTITY id, and never a cwd", async () => {
  vi.mocked(api.createMission).mockResolvedValue({ id: "msn_9" } as never);
  const onCreated = mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  // The entity list, not the folder list.
  expect(api.folders).not.toHaveBeenCalled();

  await userEvent.type(
    screen.getByTestId("new-mission-instruction"),
    "implement the thing",
  );
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-project"),
    "p1",
  );
  await userEvent.click(screen.getByTestId("new-mission-start"));

  await waitFor(() => expect(api.createMission).toHaveBeenCalled());
  const body = vi.mocked(api.createMission).mock.calls[0][0];
  expect(body.instruction).toBe("implement the thing");
  expect(body.project_id).toBe("p1");
  expect("cwd" in body).toBe(false);
  await waitFor(() =>
    expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ id: "msn_9" }),
      { focus: true },
    ),
  );
});

test("a project is REQUIRED, and the form says why before it refuses", async () => {
  // A mission created without one has no cwd, the server refuses `running` for ever, and this
  // console has no way to assign one afterwards — so allowing it was a dead end the operator
  // could only discover from a refused BEGIN (#896 review 9, finding 2).
  mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  await userEvent.type(screen.getByTestId("new-mission-instruction"), "x");

  expect(screen.getByTestId("new-mission-start")).toBeDisabled();
  expect(screen.getByTestId("new-mission-draft-note")).toHaveTextContent(
    /pick the project/i,
  );

  await userEvent.selectOptions(
    screen.getByTestId("new-mission-project"),
    "p1",
  );
  expect(screen.queryByTestId("new-mission-draft-note")).toBeNull();
  expect(screen.getByTestId("new-mission-start")).not.toBeDisabled();
});

test("the server's own refusal reaches the operator, not a generic failure", async () => {
  vi.mocked(api.createMission).mockRejectedValue(
    new ApiError(422, "that project has no folder to work in"),
  );
  mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await userEvent.type(screen.getByTestId("new-mission-instruction"), "x");
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-project"),
    "p1",
  );
  await userEvent.click(screen.getByTestId("new-mission-start"));
  await waitFor(() =>
    expect(screen.getByTestId("new-mission-error")).toHaveTextContent(
      "that project has no folder to work in",
    ),
  );
});

test("an UNREADABLE project list is not reported as an empty one", async () => {
  // Both used to render "No projects yet", which sends the operator to Settings to add a project
  // they already have. Different causes, different fixes, different sentences.
  vi.mocked(api.projectEntities).mockRejectedValue(new Error("boom"));
  mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await waitFor(() =>
    expect(screen.getByTestId("new-mission-project")).toHaveTextContent(
      /could not be read/i,
    ),
  );
  expect(screen.getByTestId("new-mission-project")).not.toHaveTextContent(
    /No projects yet/i,
  );

  // …and it does not offer a START that would create the dead end.
  await userEvent.type(screen.getByTestId("new-mission-instruction"), "x");
  expect(screen.getByTestId("new-mission-start")).toBeDisabled();
  expect(screen.getByTestId("new-mission-draft-note")).toHaveTextContent(
    /try again/i,
  );
  expect(api.createMission).not.toHaveBeenCalled();
});

test("an EMPTY project list points at where to add one", async () => {
  vi.mocked(api.projectEntities).mockResolvedValue({ projects: [] } as never);
  mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await userEvent.type(screen.getByTestId("new-mission-instruction"), "x");
  await waitFor(() =>
    expect(screen.getByTestId("new-mission-draft-note")).toHaveTextContent(
      /Settings/i,
    ),
  );
  expect(screen.getByTestId("new-mission-start")).toBeDisabled();
});

test("switching back to ASK abandons the draft instruction rather than sending it", async () => {
  mount();
  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await userEvent.type(
    screen.getByTestId("new-mission-instruction"),
    "half typed",
  );
  await userEvent.click(screen.getByTestId("composer-mode-ask"));
  // A half-typed instruction must not become a question — the two modes hold separate state.
  expect(screen.getByTestId("composer-input")).toHaveValue("");
  expect(api.createMission).not.toHaveBeenCalled();
  expect(api.pulseAsk).not.toHaveBeenCalled();
});

test("a create begun in ACTIVE does not steal the selection into the ARCHIVED rail", async () => {
  // #896 review 10, finding 4. The fence was `isCurrent(missionId)`, and this form lives on the
  // new-mission landing (the old UNTRACKED view until #948) — which the Active → Archived flip
  // does NOT unmount and whose sentinel id does not change. So "the same view is showing" stayed true across the flip while the rail
  // underneath became a different set, and a create started in Active resolved as `focus: true`
  // over an archived rail: an active mission selected into a body and a rail describing
  // Archived, which is the exact mismatch `setScope` exists to prevent.
  //
  // The harness therefore keeps `isCurrent` TRUE for the whole test. That is not an oversight —
  // it is the point. Against the reviewed shape this test cannot fail for any other reason.
  let release!: (m: unknown) => void;
  vi.mocked(api.createMission).mockReturnValue(
    new Promise((r) => {
      release = r as (m: unknown) => void;
    }) as never,
  );
  const onCreated = vi.fn();

  function Host() {
    // The console's counter: one number for view AND scope, bumped by either.
    const [scope, setScope] = useState<"active" | "archived">("active");
    const visitRef = useRef(0);
    useEffect(() => {
      visitRef.current += 1;
    }, [scope]);
    return (
      <>
        <button type="button" onClick={() => setScope("archived")}>
          show archived
        </button>
        <ControlledComposer
          missionId="untracked"
          configured
          turns={[]}
          onTurns={() => {}}
          // UNCHANGED ACROSS THE FLIP, exactly as the console's is: the sentinel is still the
          // view on screen.
          visit={() => visitRef.current}
          isVisitCurrent={(at) => at === visitRef.current}
          onCreated={onCreated}
        />
      </>
    );
  }
  render(<Host />);

  await userEvent.click(screen.getByTestId("composer-mode-new"));
  await waitFor(() => expect(api.projectEntities).toHaveBeenCalled());
  await userEvent.type(
    screen.getByTestId("new-mission-instruction"),
    "ship it",
  );
  await userEvent.selectOptions(
    screen.getByTestId("new-mission-project"),
    "p1",
  );
  await userEvent.click(screen.getByTestId("new-mission-start"));
  await waitFor(() => expect(api.createMission).toHaveBeenCalled());

  // …and the operator flips to Archived while it is still in flight.
  await userEvent.click(screen.getByText("show archived"));
  release({ id: "msn_new", title: "ship it" });

  // The mission EXISTS, so the console is still told about it — dropping it would leave a real
  // mission invisible until the next poll. Only the FOCUS is withheld.
  await waitFor(() => expect(onCreated).toHaveBeenCalled());
  expect(onCreated.mock.calls.at(-1)?.[1]).toEqual({ focus: false });
});
