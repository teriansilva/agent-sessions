import { act, renderHook } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { WhatsNewRelease } from "./releases";
import { useWhatsNew } from "./useWhatsNew";

const R: WhatsNewRelease = {
  version: "0.20.0",
  assetDir: "whatsnew/0.20",
  slides: [{ id: "intro", eyebrow: "e", title: "t", body: "b" }],
};

type Props = Parameters<typeof useWhatsNew>[0];
const props = (over: Partial<Props> = {}): Props => ({
  config: { onboarded: true, whats_new_seen: null },
  wizardOpen: false,
  bundle: "0.20.0",
  server: "0.20.0",
  updateReady: false,
  releases: [R],
  persist: vi.fn(),
  ...over,
});

function mount(initial: Props) {
  return renderHook((p: Props) => useWhatsNew(p), { initialProps: initial });
}

test("opens the newest release when it is due", () => {
  const { result } = mount(props());
  expect(result.current.release).toBe(R);
});

test("an open dialog survives a version refresh that ends eligibility — same release, nothing recorded (#977 review)", () => {
  const persist = vi.fn();
  const { result, rerender } = mount(props({ persist }));
  const opened = result.current.release;
  expect(opened).toBe(R);
  // The poll answers with the next version: the bundle is now stale, so nothing would be DUE.
  rerender(props({ persist, server: "0.20.1" }));
  expect(result.current.release).toBe(opened);
  rerender(props({ persist, server: "0.20.1", updateReady: true }));
  expect(result.current.release).toBe(opened);
  // A poll that fails is not a close either.
  rerender(props({ persist, server: null }));
  expect(result.current.release).toBe(opened);
  expect(persist).not.toHaveBeenCalled();
});

test("an open dialog survives a config refresh that already covers it, and closing it then records nothing", () => {
  const persist = vi.fn();
  const { result, rerender } = mount(props({ persist }));
  const opened = result.current.release;
  const covered = { onboarded: true, whats_new_seen: "0.20.0" };
  rerender(props({ persist, config: covered }));
  expect(result.current.release).toBe(opened);
  act(() => result.current.dismiss());
  expect(result.current.release).toBeNull();
  expect(persist).not.toHaveBeenCalled();
});

test("dismissing closes it, records the version once, and it does not reopen on the next render", () => {
  const persist = vi.fn();
  const { result, rerender } = mount(props({ persist }));
  act(() => result.current.dismiss());
  expect(result.current.release).toBeNull();
  expect(persist).toHaveBeenCalledTimes(1);
  expect(persist).toHaveBeenCalledWith("0.20.0");
  // The config still says nothing was seen (the write may have failed): the tab stays closed.
  rerender(props({ persist }));
  expect(result.current.release).toBeNull();
  expect(persist).toHaveBeenCalledTimes(1);
});

test("a manual open shows the newest notes when nothing is due, and closing covered notes records nothing", () => {
  const persist = vi.fn();
  const seen = { onboarded: true, whats_new_seen: "0.21.0" };
  const { result } = mount(props({ persist, config: seen, bundle: "0.21.0", server: "0.21.0" }));
  expect(result.current.release).toBeNull();
  act(() => result.current.open());
  expect(result.current.release).toBe(R);
  act(() => result.current.dismiss());
  expect(result.current.release).toBeNull();
  expect(persist).not.toHaveBeenCalled();
});

test("without onboarding resolved true nothing opens by itself, and a manual close records nothing", () => {
  const persist = vi.fn();
  const { result } = mount(props({ persist, config: { whats_new_seen: null } }));
  expect(result.current.release).toBeNull();
  act(() => result.current.open());
  act(() => result.current.dismiss());
  expect(persist).not.toHaveBeenCalled();
});

test("finishing setup keeps the current notes closed in this tab", () => {
  const persist = vi.fn();
  const { result, rerender } = mount(props({ persist, wizardOpen: true }));
  expect(result.current.release).toBeNull();
  act(() => result.current.markSetupDone());
  rerender(props({ persist, wizardOpen: false }));
  expect(result.current.release).toBeNull();
  expect(persist).not.toHaveBeenCalled();
});
