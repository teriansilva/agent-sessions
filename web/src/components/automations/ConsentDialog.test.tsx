import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ConsentDialog } from "./ConsentDialog";

const CONSENT = {
  detail: "widens",
  widened: ["higher mission autonomy"],
  scope_lines: [
    "Starts a mission in project p1, in /repo",
    "Autonomy: dispatches the plan without asking you",
  ],
  scope_digest: "d1",
};

test("confirm needs the tick, and sends the digest it showed (#1201)", async () => {
  const onConfirm = vi.fn();
  render(
    <ConsentDialog name="Nightly" mode="save" consent={CONSENT} onCancel={() => {}} onConfirm={onConfirm} />,
  );
  const go = screen.getByRole("button", { name: "Save changes" });
  expect(go).toBeDisabled();
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(go);
  expect(onConfirm).toHaveBeenCalledWith("d1");
});

test("the widened line is highlighted AND named, never colour alone", () => {
  render(
    <ConsentDialog name="Nightly" mode="save" consent={CONSENT} onCancel={() => {}} onConfirm={() => {}} />,
  );
  const marked = document.querySelectorAll("[data-widened]");
  expect([...marked].map((m) => m.textContent)).toEqual([
    "Autonomy: dispatches the plan without asking youWidened",
  ]);
  expect(screen.getByTestId("consent-widened")).toHaveTextContent("higher mission autonomy");
});

test("a new scope un-ticks the box: agreeing to old lines is not agreeing to new ones", async () => {
  const { rerender } = render(
    <ConsentDialog name="N" mode="enable" consent={CONSENT} onCancel={() => {}} onConfirm={() => {}} />,
  );
  await userEvent.click(screen.getByRole("checkbox"));
  expect(screen.getByRole("button", { name: "Enable automation" })).toBeEnabled();
  rerender(
    <ConsentDialog
      name="N"
      mode="enable"
      consent={{ ...CONSENT, scope_digest: "d2" }}
      onCancel={() => {}}
      onConfirm={() => {}}
    />,
  );
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Enable automation" })).toBeDisabled();
});
