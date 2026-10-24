/** Each General's voice and the call language (#1397 P3). */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api } from "../lib/api";
import type { VoiceSettings } from "../types/voice";
import { HqCallsProfiles } from "./HqCallsProfiles";

/** The page's write queue, as the settings page runs it: one after another. */
let tail: Promise<unknown> = Promise.resolve();
const inOrder = <T,>(fn: () => Promise<T>): Promise<T> => {
  const run = tail.then(fn, fn);
  tail = run.catch(() => undefined);
  return run;
};
/** The page's `saveBlock`: a queued save whose stored answer goes to `onSaved`. */
const saveBlock = (patch: Parameters<typeof api.saveVoiceSettings>[0]) =>
  inOrder(() => api.saveVoiceSettings(patch)).then((s) => {
    onSaved(s);
    return s;
  });

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { saveVoiceSettings: vi.fn(), voicePreviewContact: vi.fn() } };
});

const IDS = [
  "asia-woman", "asia-man", "europe-woman", "europe-man", "russia-woman", "russia-man", "us-woman",
  "us-man", "africa-woman", "africa-man", "southamerica-woman", "southamerica-man", "romania-woman",
];

function settings(over: Partial<VoiceSettings> = {}): VoiceSettings {
  return {
    stt: { base_url: "http://a/v1", model: "m", api_key_set: false },
    tts: { base_url: "http://b/v1", model: "m", api_key_set: false },
    voices: { woman: "aria", man: "atlas" },
    turn_taking: "natural",
    interrupt: true,
    mic: "open",
    answers: "brief",
    captions: true,
    configured: true,
    contacts: {},
    contact_defaults: Object.fromEntries(
      IDS.map((id) => [id, { group: id.endsWith("-man") ? "man" : "woman", style: id === "russia-man" ? "dramatic" : "calm", speed: id === "asia-man" ? 0.95 : 1 }]),
    ) as VoiceSettings["contact_defaults"],
    language: "auto",
    tts_styles: [],
    tts_languages: [],
    tts_voices: [],
    preview_languages: ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja", "zh", "hi", "ko"],
    stt_detects_language: false,
    tts_accepts_language: false,
    ...over,
  };
}

const onSaved = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.saveVoiceSettings).mockImplementation(async () => settings());
});

const row = (id: string) => screen.getAllByTestId("hq-calls-contact").find((r) => r.dataset.contact === id)!;

test("all 13 Generals, each control named for its General", () => {
  render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={[]} />);
  expect(screen.getAllByTestId("hq-calls-contact")).toHaveLength(13);
  expect(screen.getByRole("button", { name: "Preview General Hana Seo" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" })).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  expect(screen.getAllByRole("button", { name: /^Preview / })).toHaveLength(13);
  // Untouched rows show their defaults and offer no Reset.
  expect(within(row("asia-man")).getByTestId("hq-calls-contact-kind")).toHaveTextContent("Default");
  expect(within(row("asia-man")).getByRole("combobox", { name: "Speed for General Kenji Mori" })).toHaveDisplayValue(
    "Default · 0.95×",
  );
  expect(screen.queryByRole("button", { name: /^Reset / })).toBeNull();
});

test("the style column shows only when the server advertised styles", () => {
  const { rerender } = render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={[]} />);
  expect(screen.queryByRole("combobox", { name: /^Style for / })).toBeNull();
  expect(screen.getByTestId("hq-calls-unsupported")).toHaveTextContent("doesn't list speaking styles");
  rerender(<HqCallsProfiles saveBlock={saveBlock} settings={settings({ tts_styles: ["calm", "dramatic"] })} voiceList={[]} />);
  expect(screen.getAllByRole("combobox", { name: /^Style for / })).toHaveLength(13);
  expect(screen.getByRole("combobox", { name: "Style for General Dmitri Sokolov" })).toHaveDisplayValue("Default · dramatic");
});

test("a speed change saves the General's whole profile; Reset sends null", async () => {
  const s = settings({ contacts: { "us-woman": { voice: "nova" } } });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={[]} />);
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Speed for General Jessica Hale" }), "1.05");
  expect(api.saveVoiceSettings).toHaveBeenCalledWith({ contacts: { "us-woman": { voice: "nova", speed: 1.05 } } });
  await userEvent.click(screen.getByRole("button", { name: "Reset General Jessica Hale to default" }));
  expect(api.saveVoiceSettings).toHaveBeenLastCalledWith({ contacts: { "us-woman": null } });
  await waitFor(() => expect(onSaved).toHaveBeenCalled());
});

test("a typed voice saves when the field is left, not per keystroke", async () => {
  render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={["aria", "nova"]} />);
  const input = screen.getByRole("combobox", { name: "Voice for General Hana Seo" });
  await userEvent.type(input, "nova");
  expect(api.saveVoiceSettings).not.toHaveBeenCalled();
  await userEvent.tab();
  expect(api.saveVoiceSettings).toHaveBeenCalledExactlyOnceWith({ contacts: { "asia-woman": { voice: "nova" } } });
});

test("a saved voice the server no longer lists is Missing, with what speaks instead", () => {
  const s = settings({ contacts: { "us-woman": { voice: "nova" } }, tts_voices: ["aria", "atlas"] });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={["aria", "atlas"]} />);
  const r = row("us-woman");
  expect(within(r).getByTestId("hq-calls-contact-missing")).toHaveTextContent("Missing");
  expect(within(r).getByTestId("hq-calls-contact-missing-note")).toHaveTextContent(
    "“nova” is no longer on your server — General Hale falls back to the Woman voice (aria) until you pick one.",
  );
  // Without a discovered list nothing can be called missing.
  expect(within(row("asia-woman")).queryByTestId("hq-calls-contact-missing")).toBeNull();
});

test("voices per language: named controls, a missing one marked, remove and add save the profile", async () => {
  const s = settings({
    contacts: { "russia-man": { language_voices: { de: "kerstin", fr: "margot" } } },
    tts_voices: ["atlas", "kerstin", "jonas"],
    tts_languages: ["en", "de", "fr", "es"],
  });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={s.tts_voices} />);
  const toggle = screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" });
  expect(toggle).toHaveTextContent("Languages · 2");
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(document.getElementById(toggle.getAttribute("aria-controls")!)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Preview General Dmitri Sokolov in German" })).toBeInTheDocument();
  const fr = screen.getAllByTestId("hq-calls-lv-row").find((r) => r.dataset.lang === "fr")!;
  expect(within(fr).getByTestId("hq-calls-lv-missing")).toHaveTextContent("Missing");
  expect(fr).toHaveTextContent(
    "“margot” is no longer on your server: on French calls General Sokolov speaks with their own voice (atlas) until you pick another.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Remove the French voice for General Dmitri Sokolov" }));
  expect(api.saveVoiceSettings).toHaveBeenLastCalledWith({
    contacts: { "russia-man": { language_voices: { de: "kerstin" } } },
  });
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Language to add for General Dmitri Sokolov" }), "es");
  await userEvent.type(screen.getByRole("combobox", { name: "Voice for the new language for General Dmitri Sokolov" }), "jonas");
  await userEvent.click(screen.getByTestId("hq-calls-lv-add"));
  // Built on the latest INTENDED profile: the French voice just removed is not sent back (#1640).
  expect(api.saveVoiceSettings).toHaveBeenLastCalledWith({
    contacts: { "russia-man": { language_voices: { de: "kerstin", es: "jonas" } } },
  });
});

test("a language is offered with a Preview only where the server has a sentence in it (review 6906)", async () => {
  // No server list: the offered languages ARE the server's preview languages (Hindi, Korean
  // included), so every one a General can be given previews.
  const { unmount } = render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" }));
  const offered = Array.from(
    (screen.getByRole("combobox", { name: "Language to add for General Dmitri Sokolov" }) as HTMLSelectElement).options,
  )
    .map((o) => o.value)
    .filter(Boolean);
  expect(offered).toEqual(settings().preview_languages);
  unmount();
  // A server-listed language with no sentence: its voice is kept and used, but no Preview that
  // would only be refused; it says so instead.
  const s = settings({
    contacts: { "russia-man": { language_voices: { de: "kerstin", sw: "zuri" } } },
    tts_voices: ["atlas", "kerstin", "zuri"],
    tts_languages: ["en", "de", "sw"],
  });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={s.tts_voices} />);
  await userEvent.click(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" }));
  expect(screen.getByRole("button", { name: "Preview General Dmitri Sokolov in German" })).toBeInTheDocument();
  const sw = screen.getAllByTestId("hq-calls-lv-row").find((r) => r.dataset.lang === "sw")!;
  expect(within(sw).queryByRole("button", { name: /^Preview / })).toBeNull();
  expect(within(sw).getByTestId("hq-calls-lv-no-preview")).toHaveTextContent(
    "There’s no preview sentence in Swahili yet; Swahili calls still use this voice.",
  );
});

test("a missing saved voice previews the fallback a call would speak, unless another is typed (#1640 review 6912)", async () => {
  vi.mocked(api.voicePreviewContact).mockRejectedValue(new Error("x"));
  const s = settings({ contacts: { "us-woman": { voice: "nova" } }, tts_voices: ["aria", "atlas"] });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={["aria", "atlas"]} />);
  const preview = screen.getByRole("button", { name: "Preview General Jessica Hale" });
  await userEvent.click(preview);
  // No voice named: the server resolves the profile, and "nova" is gone, so the Woman voice.
  expect(api.voicePreviewContact).toHaveBeenLastCalledWith("us-woman", undefined, undefined);
  const input = screen.getByRole("combobox", { name: "Voice for General Jessica Hale" });
  await userEvent.clear(input);
  await userEvent.type(input, "atlas");
  await waitFor(() => expect(preview).toBeEnabled());
  await userEvent.click(preview);
  expect(api.voicePreviewContact).toHaveBeenLastCalledWith("us-woman", "atlas", undefined);
});

test("Preview plays the General's own line, with a language's voice when asked", async () => {
  vi.mocked(api.voicePreviewContact).mockRejectedValue(new Error("x"));
  const s = settings({ contacts: { "russia-man": { language_voices: { de: "kerstin" } } } });
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Preview General Hana Seo" }));
  expect(api.voicePreviewContact).toHaveBeenCalledWith("asia-woman", undefined, undefined);
  await userEvent.click(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" }));
  await userEvent.click(screen.getByRole("button", { name: "Preview General Dmitri Sokolov in German" }));
  expect(api.voicePreviewContact).toHaveBeenLastCalledWith("russia-man", "kerstin", "de") // the German line, in German;
});

test("the call language: Auto without detection is English, and says so", async () => {
  const { rerender } = render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={[]} />);
  expect(screen.getByTestId("hq-calls-language-hint")).toHaveTextContent(
    "Auto: this speech server can't tell languages apart, so calls are English. Pick a language to change it.",
  );
  rerender(<HqCallsProfiles saveBlock={saveBlock} settings={settings({ stt_detects_language: true })} voiceList={[]} />);
  expect(screen.getByTestId("hq-calls-language-hint")).toHaveTextContent("Auto: HQ answers in the language you speak");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Call language" }), "de");
  expect(api.saveVoiceSettings).toHaveBeenCalledWith({ language: "de" });
});

test("an untested speech server: calls are English and spoken confirm waits for a test", () => {
  render(<HqCallsProfiles saveBlock={saveBlock} settings={settings({ stt_detects_language: null })} voiceList={[]} />);
  const hint = screen.getByTestId("hq-calls-language-hint");
  expect(hint).toHaveTextContent("Auto: calls are English until the speech server is tested.");
  expect(hint).toHaveTextContent("Saying “confirm” works once Test both has checked the speech server");
  // Untested is not "can't detect": the unsupported note doesn't claim it.
  expect(screen.queryByText(/doesn't report the language it hears/)).toBeNull();
});

test("quick edits to one General keep each other: speed then style send both (#1640 review)", async () => {
  let release!: () => void;
  vi.mocked(api.saveVoiceSettings).mockImplementation(
    () => new Promise((resolve) => (release = () => resolve(settings({ tts_styles: ["calm", "dramatic"] })))),
  );
  render(<HqCallsProfiles saveBlock={saveBlock} settings={settings({ tts_styles: ["calm", "dramatic"] })} voiceList={[]} />);
  const speed = screen.getAllByTestId("hq-calls-contact-speed")[0];
  const style = screen.getAllByTestId("hq-calls-contact-style")[0];
  await userEvent.selectOptions(speed, "1.1");
  await userEvent.selectOptions(style, "dramatic"); // before the first save answered
  release();
  await vi.waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(2));
  release();
  const last = vi.mocked(api.saveVoiceSettings).mock.calls.at(-1)![0];
  const prof = Object.values(last.contacts ?? {})[0];
  expect(prof).toMatchObject({ speed: 1.1, style: "dramatic" });
});

test("a refused edit never rides along with a later one (#1640)", async () => {
  const s = settings({
    contacts: { "russia-man": { language_voices: { de: "kerstin", fr: "margot" } } },
    tts_voices: ["kerstin", "margot", "jonas"],
    tts_languages: ["en", "de", "fr", "es"],
  });
  let fail!: (e: Error) => void;
  vi.mocked(api.saveVoiceSettings)
    .mockImplementationOnce(() => new Promise((_, reject) => (fail = reject)) as never)
    .mockResolvedValue(s);
  render(<HqCallsProfiles saveBlock={saveBlock} settings={s} voiceList={s.tts_voices} />);
  await userEvent.click(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" }));
  await userEvent.click(screen.getByRole("button", { name: "Remove the French voice for General Dmitri Sokolov" }));
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Language to add for General Dmitri Sokolov" }), "es");
  await userEvent.type(screen.getByRole("combobox", { name: "Voice for the new language for General Dmitri Sokolov" }), "jonas");
  await userEvent.click(screen.getByTestId("hq-calls-lv-add")); // queued behind the removal
  fail(new Error("refused")); // the removal is refused …
  await vi.waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledTimes(2));
  // … so the later edit lands on what is SAVED: French stays, Spanish is added.
  expect(vi.mocked(api.saveVoiceSettings).mock.calls[1][0]).toEqual({
    contacts: { "russia-man": { language_voices: { de: "kerstin", fr: "margot", es: "jonas" } } },
  });
});

test("a voice being typed survives an earlier edit of the same row landing (#1640 second review)", async () => {
  let answer!: (s: VoiceSettings) => void;
  vi.mocked(api.saveVoiceSettings).mockImplementationOnce(() => new Promise((r) => (answer = r)));
  const { rerender } = render(<HqCallsProfiles saveBlock={saveBlock} settings={settings()} voiceList={[]} />);
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Speed for General Jessica Hale" }), "1.1");
  const input = screen.getByRole("combobox", { name: "Voice for General Jessica Hale" });
  await userEvent.type(input, "bella"); // not committed yet: the field still has focus
  const stored = settings({ contacts: { "us-woman": { speed: 1.1 } } });
  await act(async () => answer(stored));
  rerender(<HqCallsProfiles saveBlock={saveBlock} settings={stored} voiceList={[]} />);
  expect(input).toHaveValue("bella");
  await userEvent.tab(); // and it commits on top of the confirmed speed
  expect(api.saveVoiceSettings).toHaveBeenLastCalledWith({ contacts: { "us-woman": { speed: 1.1, voice: "bella" } } });
});

test("a language voice being typed survives an earlier edit of the same row landing (#1640 second review)", async () => {
  let answer!: (s: VoiceSettings) => void;
  vi.mocked(api.saveVoiceSettings).mockImplementationOnce(() => new Promise((r) => (answer = r)));
  const s0 = settings({ contacts: { "russia-man": { language_voices: { de: "kerstin" } } } });
  const { rerender } = render(<HqCallsProfiles saveBlock={saveBlock} settings={s0} voiceList={[]} />);
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Speed for General Dmitri Sokolov" }), "1.1");
  await userEvent.click(screen.getByRole("button", { name: "Voices per language for General Dmitri Sokolov" }));
  const de = screen.getByRole("combobox", { name: "German voice for General Dmitri Sokolov" });
  await userEvent.clear(de);
  await userEvent.type(de, "jonas");
  const stored = settings({ contacts: { "russia-man": { speed: 1.1, language_voices: { de: "kerstin" } } } });
  await act(async () => answer(stored));
  rerender(<HqCallsProfiles saveBlock={saveBlock} settings={stored} voiceList={[]} />);
  expect(de).toHaveValue("jonas");
});
