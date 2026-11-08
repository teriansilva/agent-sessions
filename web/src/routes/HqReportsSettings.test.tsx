/** Settings → HQ → "Sessions can report to HQ" (#1882): off by default (#1889), saved on change. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx } from "../app/config";
import { api } from "../lib/api";
import type { AppConfig } from "../types/api";
import { HqReportsSettings } from "./HqReportsSettings";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { setPrefs: vi.fn() } };
});

function renderWith(config: Partial<AppConfig>) {
  render(
    <ConfigCtx.Provider value={config as AppConfig}>
      <HqReportsSettings />
    </ConfigCtx.Provider>,
  );
}

beforeEach(() => vi.mocked(api.setPrefs).mockReset());

test("it is off when the server says nothing (the default) and saves an on", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({ hq_reports: true });
  renderWith({});
  const box = screen.getByRole("checkbox", { name: "Sessions can report to HQ" });
  expect(box).not.toBeChecked();
  await userEvent.click(box);
  expect(api.setPrefs).toHaveBeenCalledWith({ hq_reports: true });
  expect(box).toBeChecked();
  expect(screen.getByText(/Applies to sessions started after you change it/)).toBeInTheDocument();
});

test("it shows the stored value", () => {
  renderWith({ hq_reports: true });
  expect(screen.getByRole("checkbox", { name: "Sessions can report to HQ" })).toBeChecked();
});
