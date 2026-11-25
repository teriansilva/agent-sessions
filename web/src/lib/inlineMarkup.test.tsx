import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { inlineMarkup } from "./inlineMarkup";

/** #744: the recap's inline formatter. The point of these tests is the NEGATIVE space — the
 *  subset is three tokens wide (plus links to the docs site, and nowhere else) and everything else
 *  must survive as literal text, because the input is a model's summary of a transcript neither it
 *  nor we control. */

function html(text: string): string {
  const { container } = render(<div>{inlineMarkup(text)}</div>);
  return (container.firstChild as HTMLElement).innerHTML;
}

test("maps **bold** to <strong> and `code` to <code> (#744)", () => {
  render(
    <div data-testid="out">
      {inlineMarkup("**Root-caused** the race in `auth/refresh.ts`.")}
    </div>,
  );
  const out = screen.getByTestId("out");
  expect(out.querySelector("strong")).toHaveTextContent("Root-caused");
  expect(out.querySelector("code")).toHaveTextContent("auth/refresh.ts");
  expect(out).toHaveTextContent("Root-caused the race in auth/refresh.ts.");
});

test("plain text passes through untouched (#744)", () => {
  expect(html("Opened PR #482 — waiting on review.")).toBe(
    "Opened PR #482 — waiting on review.",
  );
});

// The whole reason this helper exists instead of a markdown renderer.
test("markup outside the subset stays LITERAL — no html sink (#744)", () => {
  expect(html("<script>alert(1)</script>")).toBe(
    "&lt;script&gt;alert(1)&lt;/script&gt;",
  );
  expect(html('<img src=x onerror="alert(1)">')).toBe(
    '&lt;img src=x onerror="alert(1)"&gt;',
  );
  // Images, headings and links off the docs site are NOT in the subset — they render as the
  // characters they are.
  expect(html("[click](javascript:alert(1))")).toBe(
    "[click](javascript:alert(1))",
  );
  expect(html("![x](http://evil/x.png)")).toBe("![x](http://evil/x.png)");
  expect(html("# Heading")).toBe("# Heading");
});

test("html INSIDE a subset token is still escaped (#744)", () => {
  // `code` wins the token, but its contents are a React child — never markup.
  expect(html("ran `<script>x</script>`")).toBe(
    "ran <code>&lt;script&gt;x&lt;/script&gt;</code>",
  );
  expect(html("**<b>hi</b>**")).toBe("<strong>&lt;b&gt;hi&lt;/b&gt;</strong>");
});

test("unbalanced or empty markers stay literal — no empty elements (#744)", () => {
  expect(html("2 ** 3 is not bold")).toBe("2 ** 3 is not bold");
  expect(html("**")).toBe("**");
  expect(html("`")).toBe("`");
  expect(html("**unclosed bold")).toBe("**unclosed bold");
  expect(html("`unclosed code")).toBe("`unclosed code");
});

// A runaway token would swallow the rest of the timeline; the patterns are single-line by
// construction so an unclosed marker can only ever affect its own step.
test("a token cannot span a newline (#744)", () => {
  expect(html("**one\ntwo**")).toBe("**one\ntwo**");
});

test("maps *italic* to <em> — stress in an answer's sentence", () => {
  expect(html("What I *can't* do: start a session.")).toBe(
    "What I <em>can't</em> do: start a session.",
  );
  expect(html("*whole line*")).toBe("<em>whole line</em>");
  expect(html("(*aside*), then **bold** and *x*.")).toBe(
    "(<em>aside</em>), then <strong>bold</strong> and <em>x</em>.",
  );
  expect(html("*<b>hi</b>*")).toBe("<em>&lt;b&gt;hi&lt;/b&gt;</em>");
});

test("a code span inside an italic stays code", () => {
  expect(html("*Note: run `npm ci` first.*")).toBe(
    "<em>Note: run <code>npm ci</code> first.</em>",
  );
  expect(html("the *`foo`* fn")).toBe("the <em><code>foo</code></em> fn");
});

// A star is arithmetic, a glob or a footnote far more often than it is emphasis in this app's text.
test("stars that are not emphasis stay literal", () => {
  expect(html("2 * 3 * 4")).toBe("2 * 3 * 4");
  expect(html("a*b*c")).toBe("a*b*c");
  expect(html("*foo*bar")).toBe("*foo*bar");
  // Each whitespace guard on its own: a space after the opening star, before the closing one.
  expect(html("a * b*")).toBe("a * b*");
  expect(html("rm *.log *.tmp")).toBe("rm *.log *.tmp");
  expect(html("src/*.ts and lib/*.ts")).toBe("src/*.ts and lib/*.ts");
  expect(html("* not a list here *")).toBe("* not a list here *");
  expect(html("*unclosed italic")).toBe("*unclosed italic");
  expect(html("*one\ntwo*")).toBe("*one\ntwo*");
  expect(html("`*not em*`")).toBe("<code>*not em*</code>");
});

test("a markdown link to the docs site becomes a new-tab <a> (HQ's citations)", () => {
  render(
    <div data-testid="out">
      {inlineMarkup(
        "A playbook packages that flow. ([Playbooks](https://docs.battlelabos.com/guide/playbooks#start-from-an-example))",
      )}
    </div>,
  );
  const out = screen.getByTestId("out");
  const a = out.querySelector("a");
  expect(a).toHaveTextContent("Playbooks");
  expect(a).toHaveAttribute(
    "href",
    "https://docs.battlelabos.com/guide/playbooks#start-from-an-example",
  );
  expect(a).toHaveAttribute("target", "_blank");
  expect(a).toHaveAttribute("rel", "noopener noreferrer");
  // The wrapping parens stay as text; none of the markdown syntax is left behind.
  expect(out).toHaveTextContent("A playbook packages that flow. (Playbooks)");
});

test("a bare docs URL becomes a link; trailing punctuation stays outside it", () => {
  render(
    <div data-testid="out">
      {inlineMarkup("See https://docs.battlelabos.com/guide/missions#hq-plans-every-mission.")}
    </div>,
  );
  const a = screen.getByTestId("out").querySelector("a");
  expect(a).toHaveAttribute(
    "href",
    "https://docs.battlelabos.com/guide/missions#hq-plans-every-mission",
  );
  expect(screen.getByTestId("out")).toHaveTextContent(
    "See https://docs.battlelabos.com/guide/missions#hq-plans-every-mission.",
  );
});

test("links anywhere but the docs site stay literal — no phishing link in the chrome", () => {
  for (const text of [
    "[Playbooks](https://evil.example/guide)",
    "[Playbooks](http://docs.battlelabos.com/guide)",
    "[Playbooks](https://docs.battlelabos.com.evil.example/guide)",
    "[Playbooks](javascript:alert(1))",
    "https://docs.battlelabos.com.evil.example/x",
    "https://evil.example/?https://docs.battlelabos.com/",
  ])
    expect(html(text)).not.toContain("<a");
  expect(html("[Playbooks](https://evil.example/guide)")).toBe(
    "[Playbooks](https://evil.example/guide)",
  );
});

test("a docs link's label is plain text and a link inside `code` stays code", () => {
  expect(html('[<img src=x>](https://docs.battlelabos.com/a)')).toBe(
    '<a href="https://docs.battlelabos.com/a" target="_blank" rel="noopener noreferrer">&lt;img src=x&gt;</a>',
  );
  expect(html("`https://docs.battlelabos.com/a`")).toBe(
    "<code>https://docs.battlelabos.com/a</code>",
  );
});
