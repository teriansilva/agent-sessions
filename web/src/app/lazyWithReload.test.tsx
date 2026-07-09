import { render, screen, waitFor } from "@testing-library/react";
import { Suspense } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";
import { lazyWithReload } from "./lazyWithReload";

const reload = vi.fn();
beforeEach(() => {
  sessionStorage.clear();
  reload.mockReset();
  // Replace location.reload with a spy without redefining the whole object.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
});
afterEach(() => {
  sessionStorage.clear();
});

function Ok() {
  return <div>loaded ok</div>;
}

const chunkErr = () => {
  const e = new TypeError(
    "Failed to fetch dynamically imported module: https://x/assets/Overview-abc.js",
  );
  return e;
};

function renderLazy(factory: () => Promise<{ default: React.ComponentType<unknown> }>, key: string) {
  const C = lazyWithReload(factory, key);
  return render(
    <ChunkErrorBoundary fallback={<div>chunk-fallback</div>}>
      <Suspense fallback={<div>loading</div>}>
        <C />
      </Suspense>
    </ChunkErrorBoundary>,
  );
}

test("first chunk-load error reloads once and sets the guard (#160)", async () => {
  renderLazy(() => Promise.reject(chunkErr()), "overview");
  await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  expect(sessionStorage.getItem("tr-lazy-reload:overview")).toBe("1");
  // Stays in Suspense fallback while the reload swaps the page (no chunk-fallback shown).
  expect(screen.queryByText("chunk-fallback")).not.toBeInTheDocument();
  expect(screen.getByText("loading")).toBeInTheDocument();
});

test("second chunk-load error does NOT reload — shows the boundary fallback (#160)", async () => {
  sessionStorage.setItem("tr-lazy-reload:overview", "1"); // pretend the first reload already ran
  renderLazy(() => Promise.reject(chunkErr()), "overview");
  await screen.findByText("chunk-fallback");
  expect(reload).not.toHaveBeenCalled();
});

test("successful import clears the guard so a *later* deploy can reload again (#160)", async () => {
  sessionStorage.setItem("tr-lazy-reload:overview", "1");
  renderLazy(() => Promise.resolve({ default: Ok }), "overview");
  await screen.findByText("loaded ok");
  expect(sessionStorage.getItem("tr-lazy-reload:overview")).toBeNull();
  expect(reload).not.toHaveBeenCalled();
});

test("real component errors are never reloaded — they surface like any other render error", async () => {
  // Silence the React error log noise for this expected throw.
  const spy = vi.spyOn(console, "error").mockImplementation(() => {});
  renderLazy(() => Promise.reject(new Error("real bug in module")), "overview");
  await screen.findByText("chunk-fallback");
  expect(reload).not.toHaveBeenCalled();
  expect(sessionStorage.getItem("tr-lazy-reload:overview")).toBeNull(); // guard not set
  spy.mockRestore();
});
