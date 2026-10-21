import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ConfigCtx, ConfigReloadCtx } from "../../app/config";
import { api } from "../../lib/api";
import type { AppConfig } from "../../types/api";
import { CONTACTS, contactForDay } from "./contacts";
import { HqContactGallery } from "./HqContactGallery";
import { resetHqChoiceStore } from "./hqChoiceStore";
import { useHqContact } from "./useHqContact";

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../lib/api")>("../../lib/api");
  return { ...actual, api: { setPrefs: vi.fn() } };
});

beforeEach(() => {
  vi.mocked(api.setPrefs).mockReset();
  resetHqChoiceStore();
  // Motion allowed: without matchMedia the portrait renders the still (its reduced-motion default).
  window.matchMedia = ((q: string) => ({
    matches: false,
    media: q,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: () => Promise.resolve(),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", { configurable: true, value: () => {} });
});

afterEach(() => {
  vi.useRealTimers();
  // @ts-expect-error -- jsdom has no matchMedia of its own
  delete window.matchMedia;
});

/** The app's config as the gallery sees it: a reload applies what the server last stored. */
function Live({ initial }: { initial: string }) {
  const [choice, setChoice] = useState(initial);
  const reload = async () => {
    const calls = vi.mocked(api.setPrefs).mock.calls;
    const last = calls[calls.length - 1]?.[0] as { hq_contact?: string } | undefined;
    if (last?.hq_contact) setChoice(last.hq_contact);
    return true;
  };
  return <Gallery choice={choice} reload={reload} />;
}

function Gallery({ choice, reload = async () => true }: { choice: string; reload?: () => Promise<boolean> }) {
  return (
    <ConfigCtx.Provider value={{ hq_contact: choice } as AppConfig}>
      <ConfigReloadCtx.Provider value={reload}>
        <MemoryRouter>
          <HqContactGallery subheadClass="" hintClass="" />
        </MemoryRouter>
      </ConfigReloadCtx.Provider>
    </ConfigCtx.Provider>
  );
}

test("Random first, then the 13 contacts, then Off; one radio group with one Tab stop", () => {
  render(<Gallery choice="random" />);
  const group = screen.getByRole("radiogroup", { name: "Select your primary HQ contact" });
  const radios = within(group).getAllByRole("radio");
  expect(radios.map((r) => r.dataset.key)).toEqual(["random", ...CONTACTS.map((c) => c.id), "off"]);
  expect(radios.filter((r) => r.tabIndex === 0)).toEqual([radios[0]]);
  expect(radios[0]).toHaveAttribute("aria-checked", "true");
  expect(radios[0]).toHaveTextContent("✓ Selected");
  expect(screen.getByTestId("hq-random-pick")).toHaveTextContent(`Today: ${contactForDay(new Date()).name}`);
  // Only the selected card plays; the rest are posters.
  expect(group.querySelectorAll("video")).toHaveLength(0);
});

test("arrow keys move AND select; the choice is saved to the hq_contact pref", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({} as never);
  const reload = vi.fn(async () => true);
  render(<Gallery choice="random" reload={reload} />);
  const radios = screen.getAllByRole("radio");
  radios[0].focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(api.setPrefs).toHaveBeenCalledWith({ hq_contact: CONTACTS[0].id });
  expect(radios[1]).toHaveFocus();
  await waitFor(() => expect(reload).toHaveBeenCalled());
});

test("the selected contact's card plays its loop and says Selected in words", () => {
  render(<Gallery choice="romania-woman" />);
  const card = screen.getAllByRole("radio").find((r) => r.dataset.key === "romania-woman")!;
  expect(card).toHaveAttribute("aria-checked", "true");
  expect(card.querySelector("video")).not.toBeNull();
  expect(card).toHaveTextContent("General Ileana Lupu");
  expect(card).toHaveTextContent("Romania");
  expect(card).toHaveTextContent("✓ Selected");
});

test("a refused save puts the stored choice back and says so", async () => {
  vi.mocked(api.setPrefs).mockRejectedValue(new Error("nope"));
  render(<Gallery choice="random" />);
  await userEvent.click(screen.getAllByRole("radio").find((r) => r.dataset.key === "off")!);
  expect(await screen.findByTestId("hq-contact-error")).toBeInTheDocument();
  expect(screen.getAllByRole("radio")[0]).toHaveAttribute("aria-checked", "true");
});

test("a save whose config re-read fails keeps the pick on screen, says so, and retries (Hermes on #1535)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({} as never);
  const reload = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true);
  render(<Gallery choice="random" reload={reload} />);
  const lupu = () => screen.getAllByRole("radio").find((r) => r.dataset.key === "romania-woman")!;
  await userEvent.click(lupu());
  expect(await screen.findByTestId("hq-contact-refresh-error")).toHaveTextContent("Saved");
  expect(lupu()).toHaveAttribute("aria-checked", "true"); // not reverted to the stale config
  expect(screen.queryByTestId("hq-contact-error")).toBeNull();
  await userEvent.click(screen.getByTestId("hq-contact-refresh-retry"));
  await waitFor(() => expect(screen.queryByTestId("hq-contact-refresh-error")).toBeNull());
  expect(reload).toHaveBeenCalledTimes(2);
});

test("picks are written in order and the latest wins, whatever finishes first (Hermes on #1535)", async () => {
  let releaseA!: () => void;
  vi.mocked(api.setPrefs)
    .mockImplementationOnce(() => new Promise((r) => (releaseA = () => r({} as never))))
    .mockResolvedValue({} as never);
  render(<Live initial="random" />);
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man")); // A: its write hangs
  await userEvent.click(card("asia-woman")); // B
  expect(card("asia-woman")).toHaveAttribute("aria-checked", "true");
  // B is not sent while A is in flight: the store sees A, then B.
  expect(vi.mocked(api.setPrefs).mock.calls).toEqual([[{ hq_contact: "us-man" }]]);
  await act(async () => releaseA());
  await waitFor(() =>
    expect(vi.mocked(api.setPrefs).mock.calls).toEqual([
      [{ hq_contact: "us-man" }],
      [{ hq_contact: "asia-woman" }],
    ]),
  );
  // A's completion never cleared B's pick, and the confirmed config ends on B.
  await waitFor(() => expect(card("asia-woman")).toHaveAttribute("aria-checked", "true"));
  expect(card("us-man")).toHaveAttribute("aria-checked", "false");
});

test("a third pick while one is in flight skips the superseded middle write", async () => {
  let releaseA!: () => void;
  vi.mocked(api.setPrefs)
    .mockImplementationOnce(() => new Promise((r) => (releaseA = () => r({} as never))))
    .mockResolvedValue({} as never);
  render(<Gallery choice="random" />);
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man"));
  await userEvent.click(card("asia-woman"));
  await userEvent.click(card("off"));
  await act(async () => releaseA());
  await waitFor(() => expect(vi.mocked(api.setPrefs)).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.setPrefs).mock.calls[1]).toEqual([{ hq_contact: "off" }]);
});

test("a save still in flight when Settings is left can never land after a newer pick (Hermes on #1535)", async () => {
  let releaseA!: () => void;
  vi.mocked(api.setPrefs)
    .mockImplementationOnce(() => new Promise((r) => (releaseA = () => r({} as never))))
    .mockResolvedValue({} as never);
  const first = render(<Live initial="random" />);
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man")); // A: its write hangs
  first.unmount(); // leave Settings
  render(<Live initial="random" />); // come back
  expect(card("us-man")).toHaveAttribute("aria-checked", "true"); // the pick in flight is still shown
  await userEvent.click(card("asia-woman")); // B
  expect(vi.mocked(api.setPrefs).mock.calls).toEqual([[{ hq_contact: "us-man" }]]); // B waits for A
  await act(async () => releaseA());
  await waitFor(() =>
    expect(vi.mocked(api.setPrefs).mock.calls).toEqual([
      [{ hq_contact: "us-man" }],
      [{ hq_contact: "asia-woman" }],
    ]),
  );
  await waitFor(() => expect(card("asia-woman")).toHaveAttribute("aria-checked", "true"));
});

test("a refused pick never hides an earlier pick the server accepted; every consumer follows it", async () => {
  // A: saved, but the config read fails. B: refused. What is stored is A, so the gallery AND the
  // contact HQ speaks as (`useHqContact`) must say A, not the stale config's choice.
  vi.mocked(api.setPrefs).mockResolvedValueOnce({} as never).mockRejectedValueOnce(new Error("no"));
  const reload = vi.fn(async () => false);
  render(
    <ConfigCtx.Provider value={{ hq_contact: "random" } as AppConfig}>
      <ConfigReloadCtx.Provider value={reload}>
        <MemoryRouter>
          <HqContactGallery subheadClass="" hintClass="" />
          <Who />
        </MemoryRouter>
      </ConfigReloadCtx.Provider>
    </ConfigCtx.Provider>,
  );
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man"));
  expect(await screen.findByTestId("hq-contact-refresh-error")).toBeInTheDocument();
  expect(screen.getByTestId("who")).toHaveTextContent("us-man");
  await userEvent.click(card("asia-woman"));
  expect(await screen.findByTestId("hq-contact-error")).toBeInTheDocument();
  expect(card("us-man")).toHaveAttribute("aria-checked", "true");
  expect(screen.getByTestId("who")).toHaveTextContent("us-man");
});

test("a stalled config read never holds the next pick's write (Hermes on #1535)", async () => {
  vi.mocked(api.setPrefs).mockResolvedValue({} as never);
  let releaseRead!: (ok: boolean) => void;
  const reload = vi
    .fn()
    .mockImplementationOnce(() => new Promise<boolean>((r) => (releaseRead = r)))
    .mockResolvedValue(true);
  render(<Gallery choice="random" reload={reload} />);
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man")); // A: written, its config read stalls
  await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  await userEvent.click(card("asia-woman")); // B
  await waitFor(() =>
    expect(vi.mocked(api.setPrefs).mock.calls).toEqual([
      [{ hq_contact: "us-man" }],
      [{ hq_contact: "asia-woman" }],
    ]),
  );
  await act(async () => releaseRead(true));
});

const cfgAt = (hq_contact: string, hq_contact_rev: number) =>
  ({ hq_contact, hq_contact_rev }) as AppConfig;

test("a later config the app applies retires an unconfirmed local choice everywhere (Hermes on #1535)", async () => {
  // A is written at rev 1, its own read fails; another device stores rev 2, and a refresh lands.
  vi.mocked(api.setPrefs).mockResolvedValue({ hq_contact_rev: 1 });
  const ui = withConfig(vi.fn(async () => false));
  const { rerender } = render(ui(cfgAt("random", 0)));
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man"));
  expect(await screen.findByTestId("hq-contact-refresh-error")).toBeInTheDocument();
  expect(screen.getByTestId("who")).toHaveTextContent("us-man");
  rerender(ui(cfgAt("southamerica-woman", 2)));
  await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("southamerica-woman"));
  expect(card("southamerica-woman")).toHaveAttribute("aria-checked", "true");
  expect(screen.queryByTestId("hq-contact-refresh-error")).toBeNull();
});

function withConfig(reload: () => Promise<boolean>) {
  return (cfg: AppConfig) => (
    <ConfigCtx.Provider value={cfg}>
      <ConfigReloadCtx.Provider value={reload}>
        <MemoryRouter>
          <HqContactGallery subheadClass="" hintClass="" />
          <Who />
        </MemoryRouter>
      </ConfigReloadCtx.Provider>
    </ConfigCtx.Provider>
  );
}

test("a config applied during a write that is then refused is not lost (Hermes on #1535)", async () => {
  // A at rev 1, its read fails; B in flight; another device stores C (rev 2) and a refresh lands;
  // B is refused. What is stored is C, so every consumer shows C, not A.
  let refuseB!: () => void;
  vi.mocked(api.setPrefs)
    .mockResolvedValueOnce({ hq_contact_rev: 1 })
    .mockImplementationOnce(() => new Promise((_r, rej) => (refuseB = () => rej(new Error("no")))));
  const ui = withConfig(vi.fn(async () => false));
  const { rerender } = render(ui(cfgAt("random", 0)));
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("us-man")); // A
  await screen.findByTestId("hq-contact-refresh-error");
  await userEvent.click(card("asia-woman")); // B: in flight
  rerender(ui(cfgAt("europe-man", 2))); // C applied meanwhile: newer than A's write
  await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("europe-man"));
  await act(async () => refuseB());
  expect(screen.getByTestId("who")).toHaveTextContent("europe-man");
  expect(card("europe-man")).toHaveAttribute("aria-checked", "true");
});

test("a read that predates a write never overrides it, and the read that reaches it retires it", async () => {
  let acceptB!: () => void;
  vi.mocked(api.setPrefs).mockImplementationOnce(
    () => new Promise((r) => (acceptB = () => r({ hq_contact_rev: 1 }))),
  );
  const ui = withConfig(vi.fn(async () => false));
  const { rerender } = render(ui(cfgAt("random", 0)));
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("asia-woman")); // B: in flight
  await act(async () => acceptB());
  await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("asia-woman"));
  rerender(ui(cfgAt("random", 0))); // a read issued BEFORE B's write lands after it (rev 0)
  expect(screen.getByTestId("who")).toHaveTextContent("asia-woman");
  expect(card("asia-woman")).toHaveAttribute("aria-checked", "true");
  rerender(ui(cfgAt("asia-woman", 1))); // the read that reaches B's write
  expect(screen.getByTestId("who")).toHaveTextContent("asia-woman");
  expect(card("asia-woman")).toHaveAttribute("aria-checked", "true");
});

test("a write whose response arrives after a newer server write was already applied never wins (Hermes on #1535)", async () => {
  // B commits at rev 1; another device writes C at rev 2; this tab applies C; THEN B's delayed
  // response arrives (rev 1). B's read fails. The stored choice is C, and every consumer says C.
  let respondB!: () => void;
  vi.mocked(api.setPrefs).mockImplementationOnce(
    () => new Promise((r) => (respondB = () => r({ hq_contact_rev: 1 }))),
  );
  const ui = withConfig(vi.fn(async () => false));
  const { rerender } = render(ui(cfgAt("random", 0)));
  const card = (k: string) => screen.getAllByRole("radio").find((r) => r.dataset.key === k)!;
  await userEvent.click(card("asia-woman")); // B
  rerender(ui(cfgAt("europe-man", 2))); // C applied before B's response
  await act(async () => respondB());
  await waitFor(() => expect(card("europe-man")).toHaveAttribute("aria-checked", "true"));
  expect(screen.getByTestId("who")).toHaveTextContent("europe-man");
});

function Who() {
  const { contact } = useHqContact();
  return <div data-testid="who">{contact?.id ?? "off"}</div>;
}

test("Random switches at local midnight while it is on screen", () => {
  vi.useFakeTimers({ now: new Date(2026, 9, 20, 23, 59, 0), toFake: ["Date", "setTimeout", "clearTimeout"] });
  render(
    <ConfigCtx.Provider value={{ hq_contact: "random" } as AppConfig}>
      <Who />
    </ConfigCtx.Provider>,
  );
  const tonight = contactForDay(new Date(2026, 9, 20, 12)).id;
  const tomorrow = contactForDay(new Date(2026, 9, 21, 12)).id;
  expect(screen.getByTestId("who")).toHaveTextContent(tonight);
  act(() => {
    vi.advanceTimersByTime(59_000);
  });
  expect(screen.getByTestId("who")).toHaveTextContent(tonight);
  act(() => {
    vi.advanceTimersByTime(1_001);
  });
  expect(screen.getByTestId("who")).toHaveTextContent(tomorrow);
});
