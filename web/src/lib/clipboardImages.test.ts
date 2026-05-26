import { expect, test } from "vitest";
import { imageFilesFromData } from "./clipboardImages";

const img = (name = "x.png", type = "image/png") =>
  new File([new Uint8Array([1, 2, 3])], name, { type });

// Build a DataTransfer-like object (jsdom has no full clipboard DataTransfer).
const dt = (over: Partial<{ items: unknown[]; files: unknown[] }>) =>
  ({ items: [], files: [], ...over }) as unknown as DataTransfer;

test("no data → empty", () => {
  expect(imageFilesFromData(null)).toEqual([]);
  expect(imageFilesFromData(undefined)).toEqual([]);
  expect(imageFilesFromData(dt({}))).toEqual([]);
});

test("extracts image files from clipboard items", () => {
  const f = img();
  const out = imageFilesFromData(
    dt({ items: [{ kind: "file", type: "image/png", getAsFile: () => f }] }),
  );
  expect(out).toEqual([f]);
});

test("ignores non-image items (e.g. pasted text)", () => {
  const out = imageFilesFromData(
    dt({ items: [{ kind: "string", type: "text/plain", getAsFile: () => null }] }),
  );
  expect(out).toEqual([]);
});

test("falls back to .files and filters to images", () => {
  const png = img("p.png", "image/png");
  const txt = new File(["t"], "t.txt", { type: "text/plain" });
  expect(imageFilesFromData(dt({ files: [png, txt] }))).toEqual([png]);
});

test("dedupes an image present in both items and files", () => {
  const f = img();
  const out = imageFilesFromData(
    dt({ items: [{ kind: "file", type: "image/png", getAsFile: () => f }], files: [f] }),
  );
  expect(out).toEqual([f]);
});
