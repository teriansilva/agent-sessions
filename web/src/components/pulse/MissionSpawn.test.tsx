/** The sub-agent control's BUDGET RENDERING — an unknown count is not zero (#894 review 5).
 *
 *  `spawn_live` is omitted when the server could not determine how many children a mission holds.
 *  Defaulting that to `0` told the operator every slot was free, which is the one reading the
 *  number must never support: they press the button, the server refuses, and the next honest
 *  count is distrusted too.
 *
 *  **Only the third case here is a red proof.** `null >= cap` is already `false` in JS, so the
 *  first two pass against the old `live: number` signature as well — they pin the surrounding
 *  behaviour so a later "fix" cannot quietly make unknown mean at-cap. The rendering case is the
 *  one that fails against `?? 0`, which produced "0 of 2 used".
 *
 *  The other half is that unknown must not become "at cap" either. `claim_spawn` counts and
 *  reserves in one transaction and is the only admission control there is; withholding the button
 *  on an unknown would be stricter than the thing enforcing the limit, and would hide a mission
 *  that genuinely has room.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import { MissionSpawn } from "./MissionSpawn";

afterEach(cleanup);

const props = {
  missionId: "m1",
  parentKey: "claude:11111111-1111-4111-8111-111111111111",
  engine: "claude",
  cwd: "/approved",
  cap: 2,
  busy: false,
  onChanged: () => {},
  onNote: () => {},
};

test("an UNKNOWN live count renders as unknown, never as zero", () => {
  render(<MissionSpawn {...props} live={null} />);
  const btn = screen.getByTestId("spawn-open");
  // Not disabled: the server admits or refuses, and it has not said there is no room.
  expect(btn).not.toBeDisabled();
  // …and it does not claim a number the server never produced.
  expect(btn.getAttribute("title") ?? "").not.toContain("0 of 2");
});

test("a KNOWN live count still reaches the cap and withholds the button", () => {
  render(<MissionSpawn {...props} live={2} />);
  const btn = screen.getByTestId("spawn-open");
  expect(btn).toBeDisabled();
  expect(btn.textContent).toContain("2/2");
});

test("an open card shows an unknown budget as `?`, not `0`", async () => {
  render(<MissionSpawn {...props} live={null} />);
  await userEvent.click(screen.getByTestId("spawn-open"));
  expect(screen.getByTestId("spawn-cap").textContent).toBe("? of 2 used");
});
