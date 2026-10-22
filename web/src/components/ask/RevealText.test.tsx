/** RevealText (#1171 → #1531, docs/design.md v2 §8 rule 2): **text is never held back.** The whole
 *  answer is in the DOM, formatted, the moment it arrives; only the words new since the last text
 *  fade in, and a caret rides the end while the turn is still working. */
import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";

import { blockMarkup } from "../../lib/blockMarkup";
import { MotionProvider } from "../../theme/MotionProvider";
import { useMotion } from "../../theme/motionStore";
import { paintFresh, renderedText } from "./paintFresh";
import { RevealText } from "./RevealText";

/** Under the real MotionProvider: the fade follows the ONE motion mode (#1531), which with no
 *  stored choice follows the device's reduce-motion answer stubbed below. */
const render = (ui: ReactElement) => rtlRender(ui, { wrapper: MotionProvider });

function motion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    (q: string) =>
      ({ matches: reduce && q.includes("reduce"), media: q }) as MediaQueryList,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.removeItem("tr-motion");
  delete document.documentElement.dataset.motion;
});

const TEXT = "Issue #1123 is worked by the dashboard session.";
const fresh = (el: HTMLElement) => [...el.querySelectorAll(".nj-word")].map((n) => n.textContent).join("");

test("a live answer is whole and readable at once; its words fade in, then the fade window closes", async () => {
  motion(false);
  render(<RevealText text={TEXT} testId="t" />);
  const el = screen.getByTestId("t");
  // Never held back: every character is there on the first render, once (no hidden copy).
  expect(el).toHaveTextContent(TEXT);
  expect(el.textContent).toBe(TEXT);
  expect(el).toHaveAttribute("data-revealing", "true");
  expect(fresh(el)).toBe(TEXT);
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), { timeout: 3000 });
  expect(el.querySelector(".nj-word")).toBeNull();
  expect(el.textContent).toBe(TEXT);
});

test("under reduced motion nothing fades; the text is identical", () => {
  motion(true);
  render(<RevealText text={TEXT} testId="t" />);
  const el = screen.getByTestId("t");
  expect(el).not.toHaveAttribute("data-revealing");
  expect(el.querySelector(".nj-word")).toBeNull();
  expect(el.textContent).toBe(TEXT);
});

test("a replacement fades only what is new — the agreed prefix stays put", async () => {
  motion(false);
  const { rerender } = render(<RevealText text="Probably the ws one." testId="t" />);
  const el = screen.getByTestId("t");
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), { timeout: 3000 });
  rerender(<RevealText text="Probably the ws session, confirmed." testId="t" />);
  expect(el.textContent).toBe("Probably the ws session, confirmed.");
  expect(el).toHaveAttribute("data-revealing", "true");
  expect(fresh(el)).toBe("session, confirmed.");
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), { timeout: 3000 });
});

test("an instant text — an answer read back from a kept conversation (#1443) — is plain history", () => {
  motion(false);
  render(<RevealText text={TEXT} testId="t" instant />);
  const el = screen.getByTestId("t");
  expect(el).not.toHaveAttribute("data-revealing");
  expect(el.querySelector(".nj-word")).toBeNull();
  expect(el).toHaveTextContent(TEXT);
});

test("markup formats the WHOLE text from the first render — no half-sentence painting", () => {
  motion(false);
  const text = "Two things:\n\n- **rotate** the key\n- open a follow-up";
  render(<RevealText text={text} testId="t" markup={blockMarkup} />);
  const el = screen.getByTestId("t");
  expect(el.querySelectorAll("li")).toHaveLength(2);
  expect(el.querySelector("strong")).toHaveTextContent("rotate");
  expect(el).toHaveTextContent("Two things:rotate the keyopen a follow-up");
});

test("in formatted text the caret rides inside the last block, after its last word", () => {
  motion(false);
  render(<RevealText text={"- one\n- two"} testId="t" markup={blockMarkup} instant streaming />);
  const items = screen.getByTestId("t").querySelectorAll("li");
  expect(items[1].querySelector(".nj-caret")).not.toBeNull();
  expect(items[0].querySelector(".nj-caret")).toBeNull();
});

test("a working turn shows the caret at the end; it goes when the turn settles", () => {
  motion(false);
  const { rerender } = render(<RevealText text={TEXT} testId="t" instant streaming />);
  const el = screen.getByTestId("t");
  const caret = el.querySelector(".nj-caret");
  // It rides the END of the text, after the last character.
  expect(caret?.previousSibling?.textContent?.endsWith("session.")).toBe(true);
  expect(caret?.nextSibling).toBeNull();
  rerender(<RevealText text={TEXT} testId="t" instant />);
  expect(el.querySelector(".nj-caret")).toBeNull();
});

test("a switch to Off mid-fade settles at once — the text never changes (#1531)", () => {
  motion(false);
  let setOff: () => void = () => {};
  function Switch() {
    const { setMotion } = useMotion();
    setOff = () => setMotion("off");
    return null;
  }
  render(
    <>
      <Switch />
      <RevealText text={TEXT} testId="t" />
    </>,
  );
  const el = screen.getByTestId("t");
  expect(el).toHaveAttribute("data-revealing", "true");
  act(() => setOff());
  expect(el).not.toHaveAttribute("data-revealing");
  expect(el.textContent).toBe(TEXT);
});

test("the fade boundary is measured in RENDERED text, so markdown syntax never shifts it", () => {
  const before = renderedText(blockMarkup("A **bold** start"));
  const after = renderedText(blockMarkup("A **bold** start, and more"));
  expect(before).toBe("A bold start");
  expect(after.startsWith(before)).toBe(true);
  const { container } = rtlRender(<div>{paintFresh(blockMarkup("A **bold** start, and more"), before.length, false)}</div>);
  expect([...container.querySelectorAll(".nj-word")].map((n) => n.textContent).join("")).toBe(", and more");
  expect(container.querySelector("strong")).toHaveTextContent("bold");
});
