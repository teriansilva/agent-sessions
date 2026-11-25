import { describe, expect, test } from "vitest";
import {
  classifyImage,
  classifyLink,
  createSlugger,
  fenceLanguage,
  isMarkdownPath,
  resolveRelative,
  splitFrontMatter,
} from "./markdownView";

const DOC = "/home/u/proj/docs/guide.md";

describe("isMarkdownPath", () => {
  test.each([
    ["/a/README.md", true],
    ["/a/notes.MARKDOWN", true],
    ["/a/x.mdown", true],
    ["/a/x.mkd", true],
    ["/a/page.mdx", false],
    ["/a/.md", false],
    ["/a/md", false],
    ["/a/x.md.txt", false],
  ])("%s → %s", (p, want) => expect(isMarkdownPath(p)).toBe(want));
});

describe("resolveRelative", () => {
  test("against the file's own folder, with . and .. collapsed", () => {
    expect(resolveRelative(DOC, "img/a.png")).toBe("/home/u/proj/docs/img/a.png");
    expect(resolveRelative(DOC, "./img/../a.png")).toBe("/home/u/proj/docs/a.png");
    expect(resolveRelative(DOC, "../assets/logo.svg")).toBe("/home/u/proj/assets/logo.svg");
  });
  test("a leading slash is an absolute path as written", () => {
    expect(resolveRelative(DOC, "/etc/hosts")).toBe("/etc/hosts");
  });
  test("percent-encoded names are decoded", () => {
    expect(resolveRelative(DOC, "my%20shot.png")).toBe("/home/u/proj/docs/my shot.png");
  });
  test("climbing above / is refused, not clamped", () => {
    expect(resolveRelative(DOC, "../../../../../x.png")).toBeNull();
  });
  test("a URL with a scheme or a protocol-relative URL is not a path", () => {
    expect(resolveRelative(DOC, "https://x.test/a.png")).toBeNull();
    expect(resolveRelative(DOC, "//x.test/a.png")).toBeNull();
    expect(resolveRelative(DOC, "javascript:alert(1)")).toBeNull();
  });
});

describe("classifyLink", () => {
  test("http(s) and mailto leave the app", () => {
    expect(classifyLink(DOC, "https://example.test/x")).toEqual({
      kind: "external",
      href: "https://example.test/x",
    });
    expect(classifyLink(DOC, "mailto:a@b.test").kind).toBe("external");
  });
  test("#fragment scrolls the preview", () => {
    expect(classifyLink(DOC, "#install-it")).toEqual({ kind: "anchor", id: "install-it" });
    expect(classifyLink(DOC, "#")).toEqual({ kind: "none" });
  });
  test("a relative file opens in the viewer, without its query or fragment", () => {
    expect(classifyLink(DOC, "../README.md#usage")).toEqual({
      kind: "file",
      path: "/home/u/proj/README.md",
    });
  });
  test("any other scheme is inert", () => {
    for (const h of ["javascript:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/passwd", "//evil.test/"]) {
      expect(classifyLink(DOC, h)).toEqual({ kind: "none" });
    }
    expect(classifyLink(DOC, undefined)).toEqual({ kind: "none" });
  });
});

describe("classifyImage", () => {
  test("a repo image resolves against the document", () => {
    expect(classifyImage(DOC, "shots/a.png?raw=1")).toEqual({
      kind: "repo",
      path: "/home/u/proj/docs/shots/a.png",
    });
  });
  test("a remote image is named by host, never loaded", () => {
    expect(classifyImage(DOC, "https://img.shields.io/badge/x.svg")).toEqual({
      kind: "remote",
      href: "https://img.shields.io/badge/x.svg",
      host: "img.shields.io",
    });
    expect(classifyImage(DOC, "//cdn.test/x.png").kind).toBe("remote");
  });
  test("data: and other schemes are not loaded", () => {
    expect(classifyImage(DOC, "data:image/png;base64,AAAA")).toEqual({ kind: "none" });
    expect(classifyImage(DOC, "javascript:x")).toEqual({ kind: "none" });
    expect(classifyImage(DOC, "")).toEqual({ kind: "none" });
  });
});

describe("splitFrontMatter", () => {
  test("a leading YAML block is split off", () => {
    expect(splitFrontMatter("---\ntitle: x\ntags: [a]\n---\n# Hi\n")).toEqual({
      frontMatter: "title: x\ntags: [a]",
      body: "# Hi\n",
    });
  });
  test("CRLF and a BOM", () => {
    expect(splitFrontMatter("\uFEFF---\r\na: 1\r\n---\r\nbody").frontMatter).toBe("a: 1");
  });
  test("a rule later in the document is not front matter", () => {
    const t = "# Title\n\n---\n\ntext\n---\n";
    expect(splitFrontMatter(t)).toEqual({ frontMatter: null, body: t });
  });
});

describe("createSlugger", () => {
  test("GitHub-style ids with repeats numbered", () => {
    const slug = createSlugger();
    expect(slug("Getting Started!")).toBe("getting-started");
    expect(slug("Getting Started")).toBe("getting-started-1");
    expect(slug("Getting Started")).toBe("getting-started-2");
    expect(slug("API: v2.0 (beta)")).toBe("api-v20-beta");
    expect(slug("Über uns")).toBe("über-uns");
    expect(slug("!!!")).toBe("section");
  });
});

test("fenceLanguage", () => {
  expect(fenceLanguage("language-ts")).toBe("ts");
  expect(fenceLanguage("foo language-Mermaid")).toBe("mermaid");
  expect(fenceLanguage(undefined)).toBeNull();
});
