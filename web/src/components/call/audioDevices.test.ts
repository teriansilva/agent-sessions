/** #1670 — which device a call uses: listed ids only, a label only when it is unique. */
import { beforeEach, expect, test } from "vitest";

import { choose, goneNotice, listDevices, remember, remembered } from "./audioDevices";

const mics = [
  { id: "a", label: "MacBook Pro Microphone" },
  { id: "b", label: "Jabra Evolve2 65" },
];

beforeEach(() => localStorage.clear());

test("nothing remembered: the default, nothing to say", () => {
  expect(choose("input", mics, null)).toEqual({ id: "", reason: "" });
});

test("a listed id is used as is", () => {
  expect(choose("input", mics, { id: "b", label: "Jabra Evolve2 65" })).toEqual({ id: "b", reason: "" });
});

test("an id that changed (a reboot) is found again by its UNIQUE label", () => {
  expect(choose("input", mics, { id: "old", label: "Jabra Evolve2 65" })).toEqual({ id: "b", reason: "" });
});

test("a device that is gone falls back to the default and says so", () => {
  expect(choose("output", mics, { id: "x", label: "AirPods Pro" })).toEqual({
    id: "",
    reason: "AirPods Pro isn't connected — using the default speaker.",
  });
});

test("two devices with the remembered name: never guessed, the default with the reason", () => {
  const twins = [...mics, { id: "c", label: "Jabra Evolve2 65" }];
  expect(choose("input", twins, { id: "old", label: "Jabra Evolve2 65" })).toEqual({
    id: "",
    reason: "More than one device is called Jabra Evolve2 65 — using the default microphone.",
  });
});

test("the choice is remembered per kind in this browser, and can be cleared", () => {
  remember("input", mics[1]);
  remember("output", { id: "s", label: "Speakers" });
  expect(remembered("input")).toEqual({ id: "b", label: "Jabra Evolve2 65" });
  expect(remembered("output")).toEqual({ id: "s", label: "Speakers" });
  remember("input", null);
  expect(remembered("input")).toBeNull();
  localStorage.setItem("battlelab.call.devices", "not json");
  expect(remembered("output")).toBeNull(); // a broken store is no choice, never a crash
});

test("the browser's list: its own names, plain ones without permission, nameless ids dropped", async () => {
  const md = {
    enumerateDevices: async () =>
      [
        { kind: "audioinput", deviceId: "a", label: "MacBook Pro Microphone" },
        { kind: "audioinput", deviceId: "b", label: "" },
        { kind: "audiooutput", deviceId: "s", label: "Speakers" },
        { kind: "videoinput", deviceId: "v", label: "Camera" },
        { kind: "audioinput", deviceId: "", label: "" },
      ] as MediaDeviceInfo[],
  };
  expect(await listDevices(md)).toEqual({
    inputs: [
      { id: "a", label: "MacBook Pro Microphone" },
      { id: "b", label: "Microphone 2" },
    ],
    outputs: [{ id: "s", label: "Speakers" }],
  });
});

test("the notice when the device in use disappears", () => {
  expect(goneNotice("input", "Jabra Evolve2 65")).toBe(
    "Jabra Evolve2 65 was disconnected — now using the default microphone.",
  );
});
