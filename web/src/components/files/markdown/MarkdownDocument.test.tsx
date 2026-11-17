import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import MarkdownDocument from "./MarkdownDocument";

// The diagram renderer is proven in a real browser (e2e/file-markdown-preview.spec.ts); jsdom has
// no layout for Mermaid to measure.
vi.mock("./MermaidBlock", () => ({
  MermaidBlock: ({ source }: { source: string }) => <div data-testid="mermaid">{source}</div>,
}));

const PATH = "/home/u/proj/docs/guide.md";

function renderDoc(text: string, onOpenPath = vi.fn()) {
  const r = render(<MarkdownDocument path={PATH} text={text} onOpenPath={onOpenPath} />);
  return { ...r, onOpenPath };
}

test("GFM renders: headings with ids, tables, task lists, strikethrough, footnotes", () => {
  const { container } = renderDoc(
    [
      "# Getting Started",
      "",
      "| a | b |",
      "|---|---|",
      "| 1 | 2 |",
      "",
      "- [x] done",
      "- [ ] open",
      "",
      "~~gone~~ and a note[^n].",
      "",
      "[^n]: The note.",
    ].join("\n"),
  );
  expect(screen.getByRole("heading", { level: 1, name: "Getting Started" }).id).toBe(
    "user-content-getting-started",
  );
  expect(container.querySelector("table td")?.textContent).toBe("1");
  expect(container.querySelectorAll("input[type=checkbox]")).toHaveLength(2);
  expect(container.querySelector("del")?.textContent).toBe("gone");
  expect(container.querySelector("[id='user-content-fn-n']")).not.toBeNull();
});

test("raw HTML is rendered after sanitising: script, handlers, style, frames and bad URLs are gone", () => {
  const { container } = renderDoc(
    [
      '<p align="center"><b>bold</b> <kbd>Ctrl</kbd></p>',
      "<details><summary>More</summary>hidden</details>",
      '<script>window.__x = 1</script><img src="x.png" onerror="window.__x = 2">',
      '<div style="position:fixed" onclick="evil()">styled</div>',
      '<iframe src="https://evil.test"></iframe><object data="x"></object><embed src="x">',
      '<a href="javascript:alert(1)">js</a> <a href="data:text/html,x">data</a>',
      '<form action="https://evil.test"><input name="q"></form>',
    ].join("\n\n"),
  );
  expect(container.querySelector("p[align='center'] b")?.textContent).toBe("bold");
  expect(container.querySelector("kbd")).not.toBeNull();
  expect(container.querySelector("details summary")?.textContent).toBe("More");
  for (const sel of ["script", "iframe", "object", "embed", "form", "[onerror]", "[onclick]", "[style]"]) {
    expect(container.querySelector(`[data-md-preview] ${sel}`), sel).toBeNull();
  }
  expect(screen.queryByRole("link", { name: "js" })).toBeNull();
  expect(screen.queryByRole("link", { name: "data" })).toBeNull();
  expect(screen.getByText("js").tagName).toBe("SPAN");
});

test("images: repo images resolve to /api/files/raw, remote ones are never put in an img", () => {
  const { container } = renderDoc(
    [
      "![shot](../shots/a.png)",
      "![badge](https://img.shields.io/b.svg)",
      "![cdn](//cdn.test/c.png)",
      '<img src="d.png" srcset="https://evil.test/d.png 2x">',
      '<picture><source srcset="https://evil.test/e.png"><img src="e.png" alt="pic"></picture>',
      "![inline](data:image/png;base64,AAAA)",
    ].join("\n\n"),
  );
  const srcs = [...container.querySelectorAll("img")].map((i) => i.getAttribute("src"));
  expect(srcs).toEqual([
    `/api/files/raw?path=${encodeURIComponent("/home/u/proj/shots/a.png")}`,
    `/api/files/raw?path=${encodeURIComponent("/home/u/proj/docs/d.png")}`,
    `/api/files/raw?path=${encodeURIComponent("/home/u/proj/docs/e.png")}`,
  ]);
  expect(container.querySelector("[srcset], source")).toBeNull();
  expect(container.querySelectorAll("[data-md-remote-image]")).toHaveLength(2);
  expect(screen.getByText("[image: inline]")).toBeTruthy();
});

test("links: a repo file opens through onOpenPath, #anchors stay in the document, the web opens a tab", async () => {
  const { onOpenPath } = renderDoc(
    "## Setup\n\n[next](./next.md#top) · [setup](#setup) · [site](https://example.test)",
  );
  await userEvent.click(screen.getByRole("link", { name: "next" }));
  expect(onOpenPath).toHaveBeenCalledWith("/home/u/proj/docs/next.md");
  const site = screen.getByRole("link", { name: "site" });
  expect(site.getAttribute("target")).toBe("_blank");
  expect(site.getAttribute("rel")).toBe("noopener noreferrer");
  const scroll = vi.fn();
  Element.prototype.scrollIntoView = scroll;
  await userEvent.click(screen.getByRole("link", { name: "setup" }));
  expect(scroll).toHaveBeenCalled();
  expect(onOpenPath).toHaveBeenCalledTimes(1);
});

test("front matter shows as YAML; a mermaid fence goes to the diagram block; other fences are code", async () => {
  const { container } = renderDoc(
    "---\ntitle: Guide\n---\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\n```py\nprint('hi')\n```\n",
  );
  expect(container.querySelector("[data-md-code='yaml']")?.textContent).toContain("title: Guide");
  expect(screen.getByTestId("mermaid").textContent).toContain("A --> B");
  const py = container.querySelector("[data-md-code='py']")!;
  expect(py.textContent).toContain("print('hi')");
  // Highlighted by the editor's own Python grammar: the call is a coloured span.
  await waitFor(() => expect(py.querySelectorAll("pre span").length).toBeGreaterThan(0));
});
