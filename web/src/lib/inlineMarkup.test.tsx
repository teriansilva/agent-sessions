import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { inlineMarkup } from "./inlineMarkup";

/** #744: the recap's inline formatter. The point of these tests is the NEGATIVE space — the
 *  subset is two tokens wide and everything else must survive as literal text, because the input
 *  is a model's summary of a transcript neither it nor we control. */

function html(text: string): string {
  const { container } = render(<div>{inlineMarkup(text)}</div>);
  return (container.firstChild as HTMLElement).innerHTML;
}

test("maps **bold** to <strong> and `code` to <code> (#744)", () => {
  render(
    <div data-testid="out">{inlineMarkup("**Root-caused** the race in `auth/refresh.ts`.")}</div>,
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
  expect(html("<script>alert(1)</script>")).toBe("&lt;script&gt;alert(1)&lt;/script&gt;");
  expect(html('<img src=x onerror="alert(1)">')).toBe(
    '&lt;img src=x onerror="alert(1)"&gt;',
  );
  // Links, images and headings are NOT in the subset — they render as the characters they are.
  expect(html("[click](javascript:alert(1))")).toBe("[click](javascript:alert(1))");
  expect(html("![x](http://evil/x.png)")).toBe("![x](http://evil/x.png)");
  expect(html("# Heading")).toBe("# Heading");
});

test("html INSIDE a subset token is still escaped (#744)", () => {
  // `code` wins the token, but its contents are a React child — never markup.
  expect(html("ran `<script>x</script>`")).toBe("ran <code>&lt;script&gt;x&lt;/script&gt;</code>");
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
