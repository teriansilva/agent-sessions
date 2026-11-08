/** Details → Dismiss names what the LIST ROW displayed (#1882, Hermes on #1884): the agent's
 *  report the row showed, never a report fetched later that the dialog does not show. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../../lib/api";
import type { NeedsYouDetails } from "../../types/api";
import { NeedsYouDetailsDialog } from "./NeedsYouDetailsDialog";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, api: { needsYouDetails: vi.fn(), needsYouDismiss: vi.fn() } };
});

const KEY = "claude:aaaaaaaa-0000-4000-8000-00000000000a";

function details(): NeedsYouDetails {
  return {
    id: KEY,
    title: "Move session tokens off the cookie",
    engine: "claude",
    project: { id: "p1", name: "auth-service" },
    reason: "",
    last_words: "",
    screen: "> ",
    prompt_class: "idle",
    menu: null,
    action: null,
  };
}

beforeEach(() => {
  vi.mocked(api.needsYouDetails).mockReset().mockResolvedValue(details());
  vi.mocked(api.needsYouDismiss).mockReset().mockResolvedValue({ dismissed: true, rejected: false });
});

test("Dismiss names the report the row displayed — A, even after B replaced it", async () => {
  render(
    <MemoryRouter>
      <NeedsYouDetailsDialog sessionId={KEY} shownReportId="rep-a" onClose={() => {}} onChanged={() => {}} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByTestId("needs-you-dismiss"));
  expect(api.needsYouDismiss).toHaveBeenCalledWith(KEY, undefined, { actionIds: [], reportId: "rep-a" });
});

test("opened without a displayed report, Dismiss names none", async () => {
  render(
    <MemoryRouter>
      <NeedsYouDetailsDialog sessionId={KEY} onClose={() => {}} onChanged={() => {}} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByTestId("needs-you-dismiss"));
  expect(api.needsYouDismiss).toHaveBeenCalledWith(KEY, undefined, { actionIds: [], reportId: null });
});

test("with no decision on screen, Details only HIDES — and says so", async () => {
  render(
    <MemoryRouter>
      <NeedsYouDetailsDialog sessionId={KEY} shownReportId="rep-a" onClose={() => {}} onChanged={() => {}} />
    </MemoryRouter>,
  );
  expect((await screen.findByTestId("needs-you-dismiss")).textContent).toBe("Hide for now");
});
