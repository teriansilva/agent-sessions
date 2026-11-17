import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

/** #2114: a pending image fetch or diagram render belongs to the view that asked for it. Closing
 *  or switching files aborts the fetch, makes no object URL afterwards, and revokes the ones made. */

const net = vi.hoisted(() => ({
  signals: [] as AbortSignal[],
  resolvers: [] as ((b: Blob) => void)[],
}));
vi.mock("../../../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...real,
    isApiTunneled: () => true,
    api: {
      ...real.api,
      fileBlob: (_path: string, opts: { signal?: AbortSignal } = {}) =>
        new Promise<Blob>((resolve) => {
          net.signals.push(opts.signal!);
          net.resolvers.push(resolve);
        }),
    },
  };
});

const diagram = vi.hoisted(() => ({
  pending: null as null | { resolve: (v: { svg: string }) => void; reject: (e: Error) => void; id: string },
}));
vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: (id: string) =>
      new Promise((resolve, reject) => {
        // Mermaid's render stage leaves a scratch node in <body>.
        const scratch = document.createElement("div");
        scratch.id = `d${id}`;
        document.body.appendChild(scratch);
        diagram.pending = { resolve, reject, id };
      }),
  },
}));

import { MarkdownImage } from "./MarkdownImage";
import { MermaidBlock } from "./MermaidBlock";

const created: string[] = [];
const revoked: string[] = [];
beforeEach(() => {
  net.signals.length = 0;
  net.resolvers.length = 0;
  created.length = 0;
  revoked.length = 0;
  diagram.pending = null;
  URL.createObjectURL = vi.fn(() => {
    const u = `blob:test/${created.length}`;
    created.push(u);
    return u;
  });
  URL.revokeObjectURL = vi.fn((u: string) => {
    revoked.push(u);
  });
});
afterEach(() => {
  document.body.innerHTML = "";
});

test("Home Free: an image comes through the tunnel as a blob, revoked when the viewer closes", async () => {
  const { unmount, container } = render(<MarkdownImage fromFile="/p/README.md" src="a.png" alt="a" />);
  expect(net.signals).toHaveLength(1);
  net.resolvers[0](new Blob(["png"]));
  await waitFor(() => expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:test/0"));
  unmount();
  expect(revoked).toEqual(["blob:test/0"]);
});

test("Home Free: closing while the image is in flight aborts it and makes no object URL", async () => {
  const { unmount } = render(<MarkdownImage fromFile="/p/README.md" src="a.png" alt="a" />);
  const signal = net.signals[0];
  unmount();
  expect(signal.aborted).toBe(true);
  net.resolvers[0](new Blob(["late"]));
  await Promise.resolve();
  expect(created).toEqual([]);
});

test("a diagram closed mid-render makes no image, and its scratch node does not stay behind", async () => {
  const { unmount } = render(<MermaidBlock source={"flowchart LR\n A --> B"} />);
  await waitFor(() => expect(diagram.pending).not.toBeNull());
  const { id, resolve } = diagram.pending!;
  unmount();
  resolve({ svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>' });
  await new Promise((r) => setTimeout(r, 0));
  expect(created).toEqual([]);
  expect(document.getElementById(`d${id}`)).toBeNull();
});

test("a failed render says why, shows the source, and removes its scratch node", async () => {
  render(<MermaidBlock source="not a diagram" />);
  await waitFor(() => expect(diagram.pending).not.toBeNull());
  const { id, reject } = diagram.pending!;
  reject(new Error("Parse error on line 1"));
  await screen.findByText("Diagram // Could not render");
  expect(screen.getByText("Parse error on line 1")).toBeTruthy();
  expect(screen.getByText("not a diagram")).toBeTruthy();
  expect(document.getElementById(`d${id}`)).toBeNull();
});
