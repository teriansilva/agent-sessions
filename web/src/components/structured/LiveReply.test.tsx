/** A live reply streaming INSIDE a fenced code block (#1727, Hermes on #1747): the code block shows
 *  the words RevealText marks new (`.nj-word`) and the riding caret, while Copy still copies the
 *  plain code — no caret, no fading wrappers. */
import { render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";

import { MotionProvider } from "../../theme/MotionProvider";
import { LiveReply } from "./LiveReply";

const render = (ui: ReactElement) => rtlRender(ui, { wrapper: MotionProvider });

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.motion;
});

const START = "Run this:\n\n```bash\nnpm ci\n";
const MORE = `${START}npm test -- --run\n`;

test("new words streaming into a code block fade in there, the caret rides them, Copy copies plain code", async () => {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: false, media: q }) as MediaQueryList);
  document.documentElement.dataset.motion = "full";
  const { rerender } = render(<LiveReply text={START} caret appear={false} />);
  rerender(<LiveReply text={MORE} caret appear={false} />);
  const code = screen.getByTestId("md-code").querySelector("code")!;
  // The text is all there, and the newest words carry the fade inside the block.
  expect(code.textContent).toBe("npm ci\nnpm test -- --run");
  expect([...code.querySelectorAll(".nj-word")].map((n) => n.textContent).join("")).toBe("npm test -- --run");
  // The caret rides the end of the code, inside the block — not dropped, not on a line of its own.
  const caret = code.querySelector(".nj-caret");
  expect(caret).not.toBeNull();
  expect(caret!.previousSibling?.textContent ?? "").not.toMatch(/\n$/);
  const write = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", { value: { writeText: write }, configurable: true });
  await userEvent.click(screen.getByRole("button", { name: "Copy code" }));
  expect(write).toHaveBeenCalledWith("npm ci\nnpm test -- --run");
});

test("a finished code block renders exactly its code, with no extra blank line", () => {
  render(<LiveReply text={"```js\nconst a = 1;\n```"} caret={false} appear={false} />);
  expect(screen.getByTestId("md-code").querySelector("code")!.textContent).toBe("const a = 1;");
});
