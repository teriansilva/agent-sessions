/** RevealText (#1171): the answer writes itself out, and ALWAYS finishes — the first version
 *  counted frames and froze mid-sentence on a loaded machine. Time-anchored now. */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { RevealText } from "./RevealText";

function motion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    (q: string) =>
      ({ matches: reduce && q.includes("reduce"), media: q }) as MediaQueryList,
  );
}

afterEach(() => vi.unstubAllGlobals());

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
