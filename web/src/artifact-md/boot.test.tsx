import { act } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { boot } from "./boot";

/** The renderer turns the server's page (`routes/artifacts.py` `_markdown_page`) into a document in
 *  place: the `<pre>` stays the source view, the switch appears, `body[data-md]` is the state. */

function serverPage(text: string, state = "text") {
  document.body.dataset.md = state;
  document.body.innerHTML = `<header><span id="md-switch" hidden><button type="button" data-view="rendered" aria-pressed="true">Rendered</button><button type="button" data-view="source" aria-pressed="false">Source</button></span><a href="?download=1">Download</a></header><main id="md-doc"></main><pre id="md-source" data-path="docs/r.md"></pre>`;
  document.getElementById("md-source")!.textContent = text;
}

afterEach(() => {
  document.body.innerHTML = "";
  delete document.body.dataset.md;
});

test("renders the <pre>'s text and shows the switch", async () => {
  serverPage("# Hello\n\n<b>raw</b> & more");
  await act(async () => boot(document));
  expect(document.body.dataset.md).toBe("rendered");
  expect(document.querySelector("#md-doc h1")?.textContent).toBe("Hello");
  expect(document.querySelector("#md-doc b")).toBeNull();
  expect(document.querySelector("#md-doc")?.textContent).toContain("<b>raw</b> & more");
  expect(document.getElementById("md-switch")!.hidden).toBe(false);
});

test("Source and Rendered switch the page state and the pressed button", async () => {
  serverPage("# Hello");
  await act(async () => boot(document));
  const [rendered, source] = document.querySelectorAll<HTMLButtonElement>("#md-switch button");
  source.click();
  expect(document.body.dataset.md).toBe("source");
  expect(source.getAttribute("aria-pressed")).toBe("true");
  expect(rendered.getAttribute("aria-pressed")).toBe("false");
  rendered.click();
  expect(document.body.dataset.md).toBe("rendered");
  expect(rendered.getAttribute("aria-pressed")).toBe("true");
});

test("a page the server marked big (or already failed) is left as text", async () => {
  for (const state of ["big", "failed"]) {
    serverPage("# Hello", state);
    await act(async () => boot(document));
    expect(document.body.dataset.md).toBe(state);
    expect(document.querySelector("#md-doc")?.childElementCount).toBe(0);
    expect(document.getElementById("md-switch")!.hidden).toBe(true);
  }
});
