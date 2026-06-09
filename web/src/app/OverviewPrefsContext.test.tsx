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

// Project-visibility mode (#335). No ConfigProvider here → seeds to the safe default (all mode).
function VisibilityHarness() {
  const { projectsMode, isVisible, setProjectsMode, setProjectVisible } = useOverviewPrefs();
  return (
    <>
      <div data-testid="mode">{projectsMode}</div>
      <div data-testid="vis-a">{String(isVisible("/p/a"))}</div>
      <div data-testid="vis-b">{String(isVisible("/p/b"))}</div>
      <button onClick={() => setProjectsMode("included")}>to-included</button>
      <button onClick={() => setProjectVisible("/p/a", false)}>hide-a</button>
      <button onClick={() => setProjectVisible("/p/b", true)}>include-b</button>
    </>
  );
}

test("all mode: setProjectVisible(false) hides via the denylist (#335)", async () => {
  render(
    <OverviewPrefsProvider>
      <VisibilityHarness />
    </OverviewPrefsProvider>,
  );
  expect(screen.getByTestId("mode").textContent).toBe("all");
  expect(screen.getByTestId("vis-a").textContent).toBe("true"); // visible until hidden
  await userEvent.click(screen.getByRole("button", { name: "hide-a" }));
  expect(screen.getByTestId("vis-a").textContent).toBe("false");
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_hidden: ["/p/a"] });
});

test("included mode: only allowlisted projects are visible + writes the allowlist (#335)", async () => {
  render(
    <OverviewPrefsProvider>
      <VisibilityHarness />
    </OverviewPrefsProvider>,
  );
  await userEvent.click(screen.getByRole("button", { name: "to-included" }));
  expect(screen.getByTestId("mode").textContent).toBe("included");
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_mode: "included" });
  // nothing allowlisted yet → nothing visible (a new dir never auto-appears)
  expect(screen.getByTestId("vis-a").textContent).toBe("false");
  expect(screen.getByTestId("vis-b").textContent).toBe("false");
  await userEvent.click(screen.getByRole("button", { name: "include-b" }));
  expect(screen.getByTestId("vis-b").textContent).toBe("true"); // now allowlisted
  expect(screen.getByTestId("vis-a").textContent).toBe("false"); // still not
  expect(api.setPrefs).toHaveBeenCalledWith({ projects_included: ["/p/b"] });
});
