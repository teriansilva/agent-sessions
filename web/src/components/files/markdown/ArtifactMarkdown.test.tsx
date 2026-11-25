import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ArtifactMarkdown } from "./ArtifactMarkdown";

/** The hosted Markdown renderer (#2210): the Files panel's dialect, minus raw HTML and anything
 *  that would fetch from a third party. The real-browser proof (the opaque frame, the classic
 *  bundle loading) is `e2e/appmode-artifacts.spec.ts`. */

const doc = (text: string, path = "/docs/guide.md") => render(<ArtifactMarkdown path={path} text={text} />);

test("GFM renders: headings with ids, tables, task lists, code blocks, quotes", () => {
  const { container } = doc(
    ["# Getting Started", "", "| a | b |", "|---|---|", "| 1 | 2 |", "", "- [x] done", "", "> q", "", "```yaml", "a: 1", "```"].join("\n"),
  );
  expect(screen.getByRole("heading", { level: 1, name: "Getting Started" }).id).toBe("user-content-getting-started");
  expect(container.querySelector("table td")?.textContent).toBe("1");
  expect(container.querySelectorAll("input[type=checkbox]")).toHaveLength(1);
  expect(container.querySelector("blockquote")?.textContent?.trim()).toBe("q");
  const code = container.querySelector("[data-md-code]");
  expect(code?.getAttribute("data-md-code")).toBe("yaml");
  expect(code?.querySelector("pre code")?.textContent).toBe("a: 1");
});

test("raw HTML is never parsed: it shows as the literal text it is", () => {
  const { container } = doc(
    'before <script>alert(1)</script> <img src=x onerror="alert(1)"> after\n\n<details><summary>s</summary>body</details>\n\n<iframe src="https://evil.example"></iframe>\n',
  );
  for (const tag of ["script", "img", "details", "summary", "iframe"]) {
    expect(container.querySelector(tag), tag).toBeNull();
  }
  expect(container.textContent).toContain("<script>alert(1)</script>");
  expect(container.textContent).toContain("<details><summary>s</summary>body</details>");
  const handlers = [...container.querySelectorAll("*")].flatMap((el) =>
    [...el.attributes].map((a) => a.name).filter((n) => n.startsWith("on")),
  );
  expect(handlers).toEqual([]);
});

test("links: external in a new tab without opener or referrer, relative as written, others inert", () => {
  const { container } = doc(
    "[ext](https://example.com/x) [mail](mailto:a@b.c) [rel](other.md) [up](../index.html) [abs](/etc/passwd) [js](javascript:alert(1)) [data](data:text/html,x) [anchor](#getting-started)\n",
  );
  const link = (name: string) => screen.queryByRole("link", { name });
  expect(link("ext")?.getAttribute("target")).toBe("_blank");
  expect(link("ext")?.getAttribute("rel")).toBe("noopener noreferrer");
  expect(link("mail")?.getAttribute("rel")).toBe("noopener noreferrer");
  expect(link("rel")?.getAttribute("href")).toBe("other.md");
  expect(link("rel")?.hasAttribute("target")).toBe(false);
  expect(link("up")?.getAttribute("href")).toBe("../index.html");
  expect(link("anchor")?.getAttribute("href")).toBe("#getting-started");
  // A root path would leave the artifact's prefix; a script or data URL is never a link.
  for (const name of ["abs", "js", "data"]) expect(link(name), name).toBeNull();
  expect(container.innerHTML).not.toMatch(/javascript:|data:text/);
});

test("a link that climbs above the artifact's root is inert", () => {
  doc("[out](../../x.md)\n", "/guide.md");
  expect(screen.queryByRole("link", { name: "out" })).toBeNull();
});

test("images: relative ones load from the artifact as written; remote ones are never fetched", () => {
  const { container } = doc(
    "![dot](img/dot.png) ![far](https://tracker.example/p.png) ![proto](//cdn.example/x.png) ![inline](data:image/png;base64,AAAA) ![root](/x.png)\n",
  );
  const imgs = [...container.querySelectorAll("img")];
  expect(imgs.map((i) => i.getAttribute("src"))).toEqual(["img/dot.png"]);
  const remote = [...container.querySelectorAll("[data-md-remote-image]")].map((e) => e.getAttribute("data-md-remote-image"));
  expect(remote).toEqual(["tracker.example", "cdn.example"]);
  const out = screen.getAllByRole("link", { name: /open link/i });
  expect(out.every((a) => a.getAttribute("rel") === "noopener noreferrer" && a.getAttribute("target") === "_blank")).toBe(true);
  expect(container.textContent).toContain("[image: inline]");
  expect(container.textContent).toContain("[image: root]");
});

test("front matter shows as code, not a rule and a stray heading; mermaid is code", () => {
  const { container } = doc("---\ntitle: x\n---\n# T\n\n```mermaid\ngraph TD\n```\n");
  const blocks = [...container.querySelectorAll("[data-md-code]")].map((b) => b.getAttribute("data-md-code"));
  expect(blocks).toEqual(["Front matter", "mermaid"]);
  expect(container.querySelector("hr")).toBeNull();
});
