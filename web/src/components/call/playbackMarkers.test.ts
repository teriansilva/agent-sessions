import { expect, test } from "vitest";

import { PlaybackMarkers } from "./playbackMarkers";

test("a marker is due only once the clock reaches it, in order (#1397 P2)", () => {
  const m = new PlaybackMarkers();
  m.add("a", 1.0);
  m.add("b", 2.5);
  expect(m.due(0.99)).toEqual([]);
  expect(m.due(1.0)).toEqual(["a"]);
  expect(m.due(2.4)).toEqual([]);
  expect(m.due(9)).toEqual(["b"]);
  expect(m.size).toBe(0);
});

test("a later marker never acks before an earlier one", () => {
  const m = new PlaybackMarkers();
  m.add("a", 3);
  m.add("b", 1); // queued after a: it cannot have played first
  expect(m.due(2)).toEqual([]);
  expect(m.due(3)).toEqual(["a", "b"]);
});

test("a flush drops every pending marker: none ever acks", () => {
  const m = new PlaybackMarkers();
  m.add("a", 1);
  m.add("b", 2);
  m.flush();
  expect(m.due(100)).toEqual([]);
});
