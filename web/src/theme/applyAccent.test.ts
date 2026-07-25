import { beforeEach, expect, test } from "vitest";
import { DEFAULT_ACCENT } from "./accent";
import {
  ACCENT_STORAGE_KEY,
  applyAccent,
  bootAccent,
  readStoredAccent,
  storeAccent,
} from "./applyAccent";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("style");
});

test("applyAccent sets --accent + a readable --on-accent for a custom accent", () => {
  applyAccent("#c02020"); // dark red → white ink
  const s = document.documentElement.style;
  expect(s.getPropertyValue("--accent")).toBe("#c02020");
  expect(s.getPropertyValue("--on-accent")).toBe("#ffffff");
});

test("applyAccent picks dark ink on a light accent", () => {
  applyAccent("#3fbf6f"); // light-ish green → dark ink
  expect(document.documentElement.style.getPropertyValue("--on-accent")).toBe("#0b0b0d");
});

test("applyAccent(default) clears the inline overrides so index.css rules", () => {
  applyAccent("#c02020");
  applyAccent(DEFAULT_ACCENT);
  const s = document.documentElement.style;
  expect(s.getPropertyValue("--accent")).toBe("");
  expect(s.getPropertyValue("--on-accent")).toBe("");
});

test("applyAccent coerces garbage to the default (→ clears overrides)", () => {
  applyAccent("not-a-color");
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("");
});

test("store/read round-trips a normalized accent; missing → default", () => {
  expect(readStoredAccent()).toBe(DEFAULT_ACCENT);
  storeAccent("#19B6C9");
  expect(localStorage.getItem(ACCENT_STORAGE_KEY)).toBe("#19b6c9");
  expect(readStoredAccent()).toBe("#19b6c9");
});

test("readStoredAccent coerces a corrupt cached value to the default", () => {
  localStorage.setItem(ACCENT_STORAGE_KEY, "garbage");
  expect(readStoredAccent()).toBe(DEFAULT_ACCENT);
});

test("bootAccent applies the device-cached accent", () => {
  storeAccent("#3b82f6");
  bootAccent();
  expect(document.documentElement.style.getPropertyValue("--accent")).toBe("#3b82f6");
});
