/** RevealText (#1171): the answer writes itself out, and ALWAYS finishes — the first version
 *  counted frames and froze mid-sentence on a loaded machine. Time-anchored now. */
import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";

import { MotionProvider } from "../../theme/MotionProvider";
import { useMotion } from "../../theme/motionStore";
import { RevealText } from "./RevealText";

/** Under the real MotionProvider: the reveal follows the ONE motion mode (#1531), which with no
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

test("with motion, it starts partial and finishes the whole text", async () => {
  motion(false);
  render(<RevealText text={TEXT} testId="t" />);
  const el = screen.getByTestId("t");
  expect(el).toHaveAttribute("data-revealing", "true");
  // The full text is in the accessible tree from the first frame.
  expect(el.querySelector(".sr-only")).toHaveTextContent(TEXT);
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), {
    timeout: 3000,
  });
  expect(el).toHaveTextContent(TEXT);
});

test("under reduced motion the whole text is there at once", () => {
  motion(true);
  render(<RevealText text={TEXT} testId="t" />);
  expect(screen.getByTestId("t")).not.toHaveAttribute("data-revealing");
  expect(screen.getByTestId("t")).toHaveTextContent(TEXT);
});

test("a replacement continues from where the two texts agree", async () => {
  motion(false);
  const { rerender } = render(<RevealText text="Probably the ws one." testId="t" />);
  const el = screen.getByTestId("t");
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), { timeout: 3000 });
  rerender(<RevealText text="Probably the ws session, confirmed." testId="t" />);
  // Mid-reveal, the agreed prefix is already painted.
  const painted = el.querySelector('[aria-hidden="true"]')?.textContent ?? "";
  expect(painted.startsWith("Probably the ws ")).toBe(true);
  await waitFor(() => expect(el).toHaveTextContent("Probably the ws session, confirmed."), {
    timeout: 3000,
  });
});

test("an instant text — an answer read back from a kept conversation (#1443) — shows whole at once", () => {
  motion(false);
  render(<RevealText text={TEXT} testId="t" instant />);
  const el = screen.getByTestId("t");
  expect(el).not.toHaveAttribute("data-revealing");
  expect(el).toHaveTextContent(TEXT);
});

test("a markup formats the text — whole, and while it is still writing itself out", async () => {
  motion(false);
  const md = (t: string) => <b>{t}</b>;
  render(<RevealText text={TEXT} testId="t" markup={md} />);
  const el = screen.getByTestId("t");
  // Mid-reveal, both the accessible copy and the painted partial go through it.
  expect(el.querySelector(".sr-only b")).toHaveTextContent(TEXT);
  expect(el.querySelector('[aria-hidden="true"] b')).not.toBeNull();
  await waitFor(() => expect(el).not.toHaveAttribute("data-revealing"), { timeout: 3000 });
  expect(el.querySelector(":scope > b")).toHaveTextContent(TEXT);
});

test("a switch to Off mid-reveal flushes the rest at once — never a truncated answer (#1531)", async () => {
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
  expect(el).toHaveTextContent(TEXT);
});
