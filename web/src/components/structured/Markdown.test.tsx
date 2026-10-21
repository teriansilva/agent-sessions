import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { Markdown } from "./Markdown";

test("a reply renders as elements: emphasis, inline code, lists, headings, tables, fenced code", () => {
  const { container } = render(
    <Markdown
      text={[
        "## Plan",
        "",
        "Run **all** of `npm test` first:",
        "",
        "- one",
        "- two",
        "",
        "| a | b |",
        "|---|---|",
        "| 1 | 2 |",
        "",
        "```ts",
        "const x = 1;",
        "```",
      ].join("\n")}
    />,
  );
  expect(container.querySelector("h2")).toHaveTextContent("Plan");
  expect(container.querySelector("strong")).toHaveTextContent("all");
  expect(container.querySelector("p code")).toHaveTextContent("npm test");
  expect(container.querySelectorAll("ul > li")).toHaveLength(2);
  expect(container.querySelector("table td")).toHaveTextContent("1");
  const block = screen.getByTestId("md-code");
  expect(block).toHaveTextContent("ts");
  expect(block.querySelector("pre code")).toHaveTextContent("const x = 1;");
  expect(container.textContent).not.toContain("**");
});

test("the copy button copies the block's exact code", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  render(<Markdown text={"```\nline 1\nline 2\n```"} />);
  await user.click(screen.getByRole("button", { name: "Copy code" }));
  expect(writeText).toHaveBeenCalledWith("line 1\nline 2");
  expect(await screen.findByRole("button", { name: "Copy code" })).toHaveTextContent("copied");
});

test("raw HTML never becomes markup: script, img onerror and an HTML block are inert text", () => {
  const { container } = render(
    <Markdown
      text={[
        'Hi <script>window.__pwned = 1</script> <img src=x onerror="window.__pwned = 2">',
        "",
        '<div onclick="window.__pwned = 3">block</div>',
        "",
        "<iframe src=\"https://evil.example\"></iframe>",
      ].join("\n")}
    />,
  );
  for (const tag of ["script", "img", "div[onclick]", "iframe"]) {
    expect(container.querySelector(tag)).toBeNull();
  }
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
});

test("links: only http(s) and mailto survive, and open safely; javascript: and data: are inert", () => {
  const { container } = render(
    <Markdown
      text={[
        "[ok](https://example.com/a) [mail](mailto:a@b.c)",
        "[bad](javascript:alert(1)) [data](data:text/html;base64,PHNjcmlwdD4=) [rel](/etc/passwd)",
      ].join("\n\n")}
    />,
  );
  const links = Array.from(container.querySelectorAll("a"));
  expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.com/a", "mailto:a@b.c"]);
  for (const a of links) {
    expect(a).toHaveAttribute("target", "_blank");
    expect(a).toHaveAttribute("rel", "noopener noreferrer");
  }
  expect(container.textContent).toContain("bad");
  expect(container.innerHTML).not.toMatch(/javascript:|data:text/);
});

test("an image is never fetched: it shows as its alt text", () => {
  const { container } = render(<Markdown text={"![build log](https://evil.example/t.png?leak=1)"} />);
  expect(container.querySelector("img")).toBeNull();
  expect(container).toHaveTextContent("[image: build log]");
  expect(container.innerHTML).not.toContain("evil.example");
});
