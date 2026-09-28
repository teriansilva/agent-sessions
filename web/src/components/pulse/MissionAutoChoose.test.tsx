/** The per-mission opt-in to autonomous menu answers (#1060 Phase 4), asserted on the REQUEST.
 *  Turning it on needs the yolo tier the grant depends on; turning it off never needs anything. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../../app/config";
import { ApiError, api } from "../../lib/api";
import type { AppConfig, Mission } from "../../types/api";

import { MissionAutoChoose } from "./MissionAutoChoose";

vi.mock("../../lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { setMissionAutoChoose: vi.fn() } };
});

// Braces: a function returned from beforeEach is run as its teardown.
beforeEach(() => {
  vi.mocked(api.setMissionAutoChoose)
    .mockReset()
    .mockResolvedValue({ id: "m", auto_choose: true });
});

function mount(autonomy: string, on: boolean, onChanged = vi.fn()) {
  const cfg = {
    orchestrator: { enabled: true, autonomy },
  } as unknown as AppConfig;
  render(
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={onChanged}
      />
    </ConfigCtx.Provider>,
  );
  return {
    onChanged,
    box: screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement,
  };
}

test("at yolo, ticking it opts this mission in", async () => {
  const { box, onChanged } = mount("yolo", false);
  expect(box).not.toBeChecked();
  expect(box).toBeEnabled();
  await userEvent.click(box);
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", true),
  );
  expect(onChanged).toHaveBeenCalled();
  expect(screen.queryByTestId("mission-auto-choose-why")).toBeNull();
});

test("below yolo it cannot be turned on, and says why", () => {
  const { box } = mount("suggest", false);
  expect(box).toBeDisabled();
  expect(screen.getByTestId("mission-auto-choose-why")).toHaveTextContent(
    "Needs autonomy set to yolo",
  );
});

test("below yolo an opted-in mission can still be turned OFF, and says it is paused", async () => {
  const { box } = mount("suggest", true);
  expect(box).toBeChecked();
  expect(box).toBeEnabled();
  expect(screen.getByTestId("mission-auto-choose-why")).toHaveTextContent(
    "Paused",
  );
  await userEvent.click(box);
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", false),
  );
});

test("a refusal is shown verbatim", async () => {
  vi.mocked(api.setMissionAutoChoose).mockRejectedValue(
    new ApiError(409, "an archived mission cannot answer menus on its own"),
  );
  const { box } = mount("yolo", false);
  await userEvent.click(box);
  expect(
    await screen.findByTestId("mission-auto-choose-error"),
  ).toHaveTextContent("an archived mission cannot answer menus on its own");
});

test("after a successful save the box follows the server again, whoever changes it", async () => {
  // Review 5374, finding 4: OFF → saved → server OFF → another tab turns it ON.
  vi.mocked(api.setMissionAutoChoose).mockResolvedValue({
    id: "m",
    auto_choose: false,
  });
  const cfg = {
    orchestrator: { enabled: true, autonomy: "yolo" },
  } as unknown as AppConfig;
  const view = (on: boolean) => (
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
  const { rerender } = render(view(true));
  const box = () =>
    screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement;
  await userEvent.click(box());
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", false),
  );
  expect(box()).not.toBeChecked();
  rerender(view(false)); // the refetch lands
  expect(box()).not.toBeChecked();
  rerender(view(true)); // another tab turned it back on
  expect(box()).toBeChecked();
  rerender(view(false)); // …and off again
  expect(box()).not.toBeChecked();
});

test("ON → saved → another tab turns it OFF is shown off", async () => {
  vi.mocked(api.setMissionAutoChoose).mockResolvedValue({
    id: "m",
    auto_choose: true,
  });
  const cfg = {
    orchestrator: { enabled: true, autonomy: "yolo" },
  } as unknown as AppConfig;
  const view = (on: boolean) => (
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
  const { rerender } = render(view(false));
  const box = () =>
    screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement;
  await userEvent.click(box());
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", true),
  );
  rerender(view(true));
  rerender(view(false));
  expect(box()).not.toBeChecked();
});

test("a refetch that skips the intermediate value still ends the override (review 5375)", async () => {
  // Seen ON → save OFF succeeds → another tab turns it ON before this tab's refetch lands, so the
  // refetch is a FRESH object that still says ON. The box must show ON.
  vi.mocked(api.setMissionAutoChoose).mockResolvedValue({
    id: "m",
    auto_choose: false,
  });
  const cfg = {
    orchestrator: { enabled: true, autonomy: "yolo" },
  } as unknown as AppConfig;
  const view = (on: boolean) => (
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
  const { rerender } = render(view(true));
  const box = () =>
    screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement;
  await userEvent.click(box());
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", false),
  );
  rerender(view(true)); // fresh snapshot, same value as before the click
  expect(box()).toBeChecked();
});

test("…and the inverse: seen OFF, save ON, a fresh OFF snapshot shows OFF", async () => {
  vi.mocked(api.setMissionAutoChoose).mockResolvedValue({
    id: "m",
    auto_choose: true,
  });
  const cfg = {
    orchestrator: { enabled: true, autonomy: "yolo" },
  } as unknown as AppConfig;
  const view = (on: boolean) => (
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
  const { rerender } = render(view(false));
  const box = () =>
    screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement;
  await userEvent.click(box());
  await waitFor(() =>
    expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", true),
  );
  rerender(view(false));
  expect(box()).not.toBeChecked();
});

test("a snapshot that lands WHILE the save is in flight does not undo the click", async () => {
  let release!: (v: { id: string; auto_choose: boolean }) => void;
  vi.mocked(api.setMissionAutoChoose).mockImplementation(
    () => new Promise((r) => (release = r)),
  );
  const cfg = {
    orchestrator: { enabled: true, autonomy: "yolo" },
  } as unknown as AppConfig;
  const view = (on: boolean) => (
    <ConfigCtx.Provider value={cfg}>
      <MissionAutoChoose
        mission={{ id: "m", auto_choose: on } as Mission}
        onChanged={vi.fn()}
      />
    </ConfigCtx.Provider>
  );
  const { rerender } = render(view(false));
  const box = () =>
    screen.getByTestId("mission-auto-choose-toggle") as HTMLInputElement;
  await userEvent.click(box());
  rerender(view(false)); // a poll that started before the save committed
  expect(box()).toBeChecked();
  release({ id: "m", auto_choose: true });
  await waitFor(() => expect(box()).toBeEnabled());
  rerender(view(true));
  expect(box()).toBeChecked();
});


test.each([true, false])("mission grant wins over a divergent legacy projection: %s", async (enabled) => {
  const cfg = {
    orchestrator: { enabled: !enabled, autonomy: "yolo" },
    automation: {
      version: 1,
      legacy_compatible: false,
      session: { enabled: !enabled, autonomy: "yolo" },
      mission: { enabled, autonomy: "yolo" },
    },
  } as unknown as AppConfig;
  render(<ConfigCtx.Provider value={cfg}>
    <MissionAutoChoose mission={{ id: "m", auto_choose: false } as Mission} onChanged={vi.fn()} />
  </ConfigCtx.Provider>);
  const box = screen.getByTestId("mission-auto-choose-toggle");
  if (enabled) {
    expect(box).toBeEnabled();
    await userEvent.click(box);
    await waitFor(() => expect(api.setMissionAutoChoose).toHaveBeenCalledWith("m", true));
  } else {
    expect(box).toBeDisabled();
    expect(api.setMissionAutoChoose).not.toHaveBeenCalled();
  }
});
