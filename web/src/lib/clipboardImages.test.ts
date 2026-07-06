import { afterEach, expect, test, vi } from "vitest";
import { imageFilesFromAsyncClipboard, imageFilesFromData } from "./clipboardImages";

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

// #530 — deferred clipboard backends (Windows Chrome 149's lazy format reads) can leave
// `DataTransferItem.type` empty at enumeration time even though `getAsFile()` yields a real
// image, or hand out a File that lost its MIME type but kept the image filename.

test("materializes a file-kind item whose enumeration-time type is empty (#530)", () => {
  const f = img();
  const out = imageFilesFromData(dt({ items: [{ kind: "file", type: "", getAsFile: () => f }] }));
  expect(out).toEqual([f]);
});

test("accepts a typeless File with an image filename extension (#530)", () => {
  const f = img("Screenshot_20260706-091500.png", "");
  expect(imageFilesFromData(dt({ files: [f] }))).toEqual([f]);
});

test("still rejects a materialized non-image file", () => {
  const pdf = new File(["%"], "doc.pdf", { type: "application/pdf" });
  const out = imageFilesFromData(
    dt({ items: [{ kind: "file", type: "", getAsFile: () => pdf }] }),
  );
  expect(out).toEqual([]);
});

test("survives getAsFile() returning null", () => {
  expect(
    imageFilesFromData(dt({ items: [{ kind: "file", type: "", getAsFile: () => null }] })),
  ).toEqual([]);
});

// #530 — the async-clipboard fallback for paste events whose DataTransfer is unusable.

afterEach(() => {
  vi.unstubAllGlobals();
});

const stubClipboardRead = (impl: () => Promise<unknown>) =>
  vi.stubGlobal("navigator", { clipboard: { read: impl } });

test("imageFilesFromAsyncClipboard lifts image items into Files", async () => {
  const bytes = new Blob([new Uint8Array([9, 9])], { type: "image/png" });
  stubClipboardRead(() =>
    Promise.resolve([
      { types: ["text/html"], getType: () => Promise.reject(new Error("unused")) },
      { types: ["text/plain", "image/png"], getType: () => Promise.resolve(bytes) },
    ]),
  );
  const out = await imageFilesFromAsyncClipboard();
  expect(out).toHaveLength(1);
  expect(out[0].type).toBe("image/png");
  expect(out[0].name).toBe("clipboard.png");
});

test("imageFilesFromAsyncClipboard fails soft on rejection / missing API", async () => {
  stubClipboardRead(() => Promise.reject(new DOMException("denied", "NotAllowedError")));
  expect(await imageFilesFromAsyncClipboard()).toEqual([]);
  vi.stubGlobal("navigator", {}); // no clipboard at all (insecure context)
  expect(await imageFilesFromAsyncClipboard()).toEqual([]);
});
