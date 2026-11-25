/** #1670 — the call's Audio sheet: the browser's devices as named choices, a refused switch keeps
 *  what is in use, a browser that can't pick a speaker says how, and an unplug notice shows. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { remembered } from "./audioDevices";
import { AudioSheet } from "./AudioSheet";
import { SUPERSEDED, type DeviceAudio } from "./callAudio";

function fakeApi(over: Partial<DeviceAudio> = {}) {
  const state = { input: "default", output: "spk" };
  let notice: ((m: string) => void) | null = null;
  const api: DeviceAudio & { notify: (m: string) => void } = {
    devices: async () => ({
      inputs: [
        { id: "default", label: "Default — MacBook Pro Microphone" },
        { id: "jabra", label: "Jabra Evolve2 65" },
      ],
      outputs: [
        { id: "spk", label: "MacBook Pro Speakers" },
        { id: "lg", label: "LG UltraFine Display Audio" },
      ],
    }),
    current: () => ({ ...state }),
    setInput: vi.fn(async (id: string) => {
      state.input = id;
      return null;
    }),
    setOutput: vi.fn(async (id: string) => {
      state.output = id;
      return null;
    }),
    canSelectOutput: () => true,
    canPickOutput: () => false,
    pickOutput: vi.fn(async () => ({ result: SUPERSEDED, device: null })),
    onNotice: (fn) => {
      notice = fn;
    },
    notify: (m) => notice?.(m),
    ...over,
  };
  return api;
}

beforeEach(() => localStorage.clear());

test("the browser's microphones and speakers, by name, with the ones in use checked", async () => {
  render(<AudioSheet api={fakeApi()} id="a" />);
  const mic = await screen.findByRole("group", { name: "Microphone" });
  expect(within(mic).getByRole("radio", { name: "Default — MacBook Pro Microphone" })).toBeChecked();
  expect(within(mic).getByRole("radio", { name: "Jabra Evolve2 65" })).not.toBeChecked();
  const spk = screen.getByRole("group", { name: "Speaker" });
  expect(within(spk).getByRole("radio", { name: "MacBook Pro Speakers" })).toBeChecked();
});

test("choosing a device switches to it at once and remembers it on this device", async () => {
  const api = fakeApi();
  render(<AudioSheet api={api} id="a" />);
  await userEvent.click(await screen.findByRole("radio", { name: "Jabra Evolve2 65" }));
  expect(api.setInput).toHaveBeenCalledWith("jabra");
  await waitFor(() => expect(screen.getByRole("radio", { name: "Jabra Evolve2 65" })).toBeChecked());
  expect(remembered("input")).toEqual({ id: "jabra", label: "Jabra Evolve2 65" });
  await userEvent.click(screen.getByRole("radio", { name: "LG UltraFine Display Audio" }));
  expect(api.setOutput).toHaveBeenCalledWith("lg");
  expect(remembered("output")).toEqual({ id: "lg", label: "LG UltraFine Display Audio" });
});

test("a refused switch keeps the device in use checked and says why; nothing is remembered", async () => {
  const api = fakeApi({
    setOutput: vi.fn(async () => "Couldn’t switch to that speaker — the browser refused. Still playing on the current one."),
  });
  render(<AudioSheet api={api} id="a" />);
  await userEvent.click(await screen.findByRole("radio", { name: "LG UltraFine Display Audio" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Still playing on the current one.");
  expect(screen.getByRole("radio", { name: "MacBook Pro Speakers" })).toBeChecked();
  expect(screen.getByRole("radio", { name: "LG UltraFine Display Audio" })).not.toBeChecked();
  expect(remembered("output")).toBeNull();
});

test("a browser that can't pick the speaker says how to switch it instead of offering a list", async () => {
  render(<AudioSheet api={fakeApi({ canSelectOutput: () => false })} id="a" />);
  expect(await screen.findByTestId("call-audio-output-note")).toHaveTextContent(
    "Your device picks the speaker",
  );
  expect(screen.queryByRole("group", { name: "Speaker" })).toBeNull();
  expect(screen.getByRole("group", { name: "Microphone" })).toBeInTheDocument();
});

test("an unplug mid-call shows the fallback notice and re-reads the devices", async () => {
  const api = fakeApi();
  render(<AudioSheet api={api} id="a" />);
  await screen.findByRole("group", { name: "Microphone" });
  act(() => api.notify("Jabra Evolve2 65 was disconnected — now using the default microphone."));
  expect(await screen.findByRole("status")).toHaveTextContent("Jabra Evolve2 65 was disconnected");
});

test("an overtaken choice is neither remembered nor shown as an error (#1678 review)", async () => {
  const api = fakeApi({ setInput: vi.fn(async () => SUPERSEDED) });
  render(<AudioSheet api={api} id="a" />);
  await userEvent.click(await screen.findByRole("radio", { name: "Jabra Evolve2 65" }));
  expect(api.setInput).toHaveBeenCalledWith("jabra");
  expect(remembered("input")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("where the browser has a speaker picker, a tap on 'Another speaker…' opens it at once (#1670)", async () => {
  const api = fakeApi({
    canPickOutput: () => true,
    pickOutput: vi.fn(async () => ({ result: null, device: { id: "bt", label: "AirPods Pro" } })),
  });
  render(<AudioSheet api={api} id="a" />);
  const pickRow = await screen.findByTestId("call-audio-output-pick");
  act(() => pickRow.click()); // synchronously, inside the tap's gesture
  expect(api.pickOutput).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(remembered("output")).toEqual({ id: "bt", label: "AirPods Pro" }));
});

test("a cancelled or refused speaker picker changes nothing and shows no error", async () => {
  const api = fakeApi({
    canPickOutput: () => true,
    pickOutput: vi.fn(async () => ({ result: SUPERSEDED, device: null })),
  });
  render(<AudioSheet api={api} id="a" />);
  await userEvent.click(await screen.findByTestId("call-audio-output-pick"));
  await waitFor(() => expect(api.pickOutput).toHaveBeenCalled());
  expect(remembered("output")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("no speaker picker in the browser: no extra row", async () => {
  render(<AudioSheet api={fakeApi()} id="a" />);
  await screen.findByRole("group", { name: "Speaker" });
  expect(screen.queryByTestId("call-audio-output-pick")).toBeNull();
});
