import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";
import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { ApiLookProvider } from "./ApiLookProvider";
import { API_LOOK_STORAGE_KEY, useApiLook } from "./apiLookStore";

vi.mock("../lib/api", () => ({ api: { setApiLook: vi.fn().mockResolvedValue({}) } }));

const CONFIG = { csrf: "x", new_session_engines: [], terminal_backend: "ws" } as AppConfig;
const NORD = { palette: "nord", background: "grid", body_font: "serif", code_font: "app", text_size: 16 };

function Harness() {
  const { look, setLook } = useApiLook();
  return (
    <button type="button" onClick={() => setLook({ ...look, palette: "sepia" })}>
      {JSON.stringify(look)}
    </button>
  );
}
const renderWith = (config: AppConfig | null) =>
  render(
    <ConfigCtx.Provider value={config}>
      <ApiLookProvider>
        <Harness />
      </ApiLookProvider>
    </ConfigCtx.Provider>,
  );
const shown = () => JSON.parse(screen.getByRole("button").textContent!);

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

test("a change is cached on the device and persisted", async () => {
  renderWith(null);
  await userEvent.click(screen.getByRole("button"));
  expect(shown().palette).toBe("sepia");
  expect(JSON.parse(localStorage.getItem(API_LOOK_STORAGE_KEY)!).palette).toBe("sepia");
  expect(api.setApiLook).toHaveBeenCalledWith(expect.objectContaining({ palette: "sepia" }));
});

test("a device with no choice is seeded from the server once", async () => {
  renderWith({ ...CONFIG, api_look: NORD });
  await waitFor(() => expect(shown()).toEqual(NORD));
});

test("a valid local choice wins over the server", async () => {
  localStorage.setItem(API_LOOK_STORAGE_KEY, JSON.stringify({ ...NORD, palette: "contrast" }));
  renderWith({ ...CONFIG, api_look: NORD });
  await new Promise((r) => setTimeout(r, 0));
  expect(shown().palette).toBe("contrast");
});

test("a partly-bad cache keeps its good fields; a non-JSON cache lets the server seed", async () => {
  localStorage.setItem(API_LOOK_STORAGE_KEY, JSON.stringify({ ...NORD, background: "plaid" }));
  const { unmount } = renderWith(null);
  expect(shown()).toEqual({ ...NORD, background: "none" });
  unmount();
  localStorage.setItem(API_LOOK_STORAGE_KEY, "{not json");
  renderWith({ ...CONFIG, api_look: NORD });
  await waitFor(() => expect(shown()).toEqual(NORD));
});

test("rapid changes are saved in order, latest wins — an older request can't settle last", async () => {
  const held: Array<{ palette: string; release: () => void }> = [];
  vi.mocked(api.setApiLook).mockImplementation(
    (look) => new Promise((resolve) => held.push({ palette: look.palette, release: () => resolve({}) })),
  );
  function Picker() {
    const { look, setLook } = useApiLook();
    return (
      <>
        {(["nord", "sepia", "contrast"] as const).map((id) => (
          <button key={id} type="button" onClick={() => setLook({ ...look, palette: id })}>
            {id}
          </button>
        ))}
      </>
    );
  }
  render(
    <ConfigCtx.Provider value={null}>
      <ApiLookProvider>
        <Picker />
      </ApiLookProvider>
    </ConfigCtx.Provider>,
  );
  for (const id of ["nord", "sepia", "contrast"])
    await userEvent.click(screen.getByRole("button", { name: id }));
  expect(held.map((h) => h.palette)).toEqual(["nord"]);
  held[0].release();
  await waitFor(() => expect(held.map((h) => h.palette)).toEqual(["nord", "contrast"]));
  held[1].release();
  await new Promise((r) => setTimeout(r, 0));
  expect(vi.mocked(api.setApiLook).mock.calls.at(-1)![0].palette).toBe("contrast");
});
