import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "../lib/api";
import { OverviewPrefsProvider } from "./OverviewPrefsContext";
import { useOverviewPrefs } from "./overviewPrefs";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ...actual.api, setPrefs: vi.fn().mockResolvedValue({}) } };
});

beforeEach(() => vi.clearAllMocks());

// A stand-in for the canvas: reads the shared excluded set + (like Settings) saves one.
function Harness() {
  const { excluded, expanded, setExcluded, toggle } = useOverviewPrefs();
  return (
    <>
      <div data-testid="excluded">{[...excluded].join(",")}</div>
      <div data-testid="expanded">{[...expanded].join(",")}</div>
      <button onClick={() => setExcluded(["/p/secret"])}>exclude</button>
      <button onClick={() => toggle("/p/one")}>toggle</button>
    </>
  );
}

test("a save is visible to a canvas-like consumer immediately + persists (#144)", async () => {
  render(
    <OverviewPrefsProvider>
      <Harness />
    </OverviewPrefsProvider>,
  );
  expect(screen.getByTestId("excluded").textContent).toBe("");
  await userEvent.click(screen.getByRole("button", { name: "exclude" }));
  // The SAME context state a canvas reads now reflects the exclusion — no page reload.
  expect(screen.getByTestId("excluded").textContent).toBe("/p/secret");
  // The provider now writes the new `projects_hidden` key (#174); the legacy
  // `overview_excluded` is still accepted by the server but no client sends it any more.
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_hidden: ["/p/secret"] });
});

test("toggling a cluster updates shared expanded state + persists (#144)", async () => {
  render(
    <OverviewPrefsProvider>
      <Harness />
    </OverviewPrefsProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "toggle" }));
  expect(screen.getByTestId("expanded").textContent).toBe("/p/one");
  expect(api.setPrefs).toHaveBeenCalledWith({ overview_expanded: ["/p/one"] });
});
