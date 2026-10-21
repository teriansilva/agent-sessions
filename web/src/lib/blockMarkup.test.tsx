import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import { blockMarkup } from "./blockMarkup";

/** #1168: the NEEDS YOU last words render their markdown structure — and nothing outside the
 *  enumerated subset becomes markup, because the input is agent text nobody here controls. */

function html(text: string): string {
  const { container } = render(<div>{blockMarkup(text)}</div>);
  return (container.firstChild as HTMLElement).innerHTML;
}

test("a bulleted report with bold labels and code renders as a list (#1168)", () => {
  const out = html(
    "Status:\n\n- **Checkouts:** under `~/agentwork/`.\n- **Next phases:** P5 waits on #832.",
  );
  expect(out).toBe(
    "<p>Status:</p><ul>" +
      "<li><strong>Checkouts:</strong> under <code>~/agentwork/</code>.</li>" +
      "<li><strong>Next phases:</strong> P5 waits on #832.</li></ul>",
  );
});

test("ordered lists keep their start, and indented lines continue an item", () => {
  expect(html("3. one\n   more\n4. two")).toBe(
    '<ol start="3"><li>one\nmore</li><li>two</li></ol>',
  );
});

test("headings become a bold line, fences a code block — closed or not", () => {
  expect(html("## Done\n```sh\nrm **x**\n```\nafter")).toBe(
    "<p><strong>Done</strong></p><pre><code>rm **x**</code></pre><p>after</p>",
  );
  expect(html("```\nunclosed")).toBe("<pre><code>unclosed</code></pre>");
});

test("a tail-capped message that opens mid-sentence is a plain paragraph", () => {
  expect(html("…nd the fix is in.\n- next")).toBe(
    "<p>…nd the fix is in.</p><ul><li>next</li></ul>",
  );
});

test("links, images and raw HTML stay literal text — no html sink (#1168)", () => {
  const out = html(
    '- [click](javascript:alert(1)) ![x](y) <img src=x onerror="1"><script>1</script>',
  );
  expect(out).not.toContain("<a");
  expect(out).not.toContain("<img");
  expect(out).not.toContain("<script");
  expect(out).toContain("&lt;script&gt;");
});

test("plain text passes through as one paragraph", () => {
  expect(html("Which option do you want?")).toBe(
    "<p>Which option do you want?</p>",
  );
});
