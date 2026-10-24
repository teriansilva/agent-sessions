/** Settings → AI → HQ calls (#1397 P1, P3, P4): tokens are never shown (a stored one reads "Saved"),
 *  a blank field keeps it, Remove clears it; Test both checks what is typed without saving; the
 *  server's refusal is shown; conversation settings save at once. P3: each General's voice, the
 *  call language, and what a save records about the server. P4: speech on this machine. */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { VoiceCapabilities, VoiceLocal, VoiceSettings } from "../types/voice";
import { HqCallsSettings } from "./HqCallsSettings";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      voiceSettings: vi.fn(),
      saveVoiceSettings: vi.fn(),
      testVoice: vi.fn(),
      voiceLists: vi.fn(),
      voicePreview: vi.fn(),
      voiceCapabilities: vi.fn(),
      voicePreviewContact: vi.fn(),
      voiceLocal: vi.fn(),
      useLocalVoice: vi.fn(),
    },
  };
});

const SETTINGS: VoiceSettings = {
  stt: { base_url: "http://gpu-1.lan:8000/v1", model: "whisper-1", api_key_set: false },
  tts: { base_url: "http://gpu-1.lan:8880/v1", model: "tts", api_key_set: true },
  voices: { woman: "aria", man: "atlas" },
  turn_taking: "natural",
  interrupt: true,
  mic: "open",
  answers: "brief",
  captions: true,
  configured: true,
  contacts: {},
  contact_defaults: Object.fromEntries(
    [
      "asia-woman", "asia-man", "europe-woman", "europe-man", "russia-woman", "russia-man",
      "us-woman", "us-man", "africa-woman", "africa-man", "southamerica-woman", "southamerica-man",
      "romania-woman",
    ].map((id) => [
      id,
      { group: id.endsWith("-man") ? "man" : "woman", style: id === "asia-woman" ? "calm" : "dramatic", speed: id === "asia-man" ? 0.95 : 1 },
    ]),
  ) as VoiceSettings["contact_defaults"],
  language: "auto",
  tts_styles: [],
  tts_languages: [],
  tts_voices: [],
  preview_languages: ["en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja", "zh", "hi", "ko"],
  stt_detects_language: false,
  tts_accepts_language: false,
};

const LOCAL: VoiceLocal = {
  installed: { runtime: false, server: false },
  token: false,
  unit: null,
  systemd: true,
  stt: null,
  last_error: null,
  stale: null,
  url: "http://127.0.0.1:8766/v1",
  running: false,
  in_use: false,
  voices: { woman: "af_heart", man: "am_michael" },
  limits: "hard",
};

const CAPS: VoiceCapabilities = {
  configured: true,
  runtime: true,
  available: true,
  reason: "",
  captions: true,
  mic: "open",
  active_call: null,
  destinations: {
    stt: "lan",
    tts: "lan",
    ai: "remote",
    stt_host: "gpu-1.lan:8000",
    tts_host: "gpu-1.lan:8880",
    ai_host: "ai.example.com",
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.voiceSettings).mockResolvedValue(SETTINGS);
  vi.mocked(api.voiceCapabilities).mockResolvedValue(CAPS);
  vi.mocked(api.saveVoiceSettings).mockImplementation(async () => SETTINGS);
  vi.mocked(api.voiceLocal).mockResolvedValue(LOCAL);
});

function renderPage() {
  return render(
    <MemoryRouter>
      <HqCallsSettings />
    </MemoryRouter>,
  );
}

test("a stored token reads Saved — never a suffix — and an unset one offers a field", async () => {
  renderPage();
  expect(await screen.findByTestId("hq-calls-tts-token-state")).toHaveTextContent("Saved");
  expect(screen.getByTestId("hq-calls-stt-token")).toHaveValue("");
  expect(document.body.textContent).not.toMatch(/sk-|\*\*\*/);
});

test("saving with a blank token keeps it: no api_key in the patch", async () => {
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.stt).toEqual({ base_url: "http://gpu-1.lan:8000/v1", model: "whisper-1" });
  expect(patch.tts).not.toHaveProperty("api_key");
  expect(patch.voices).toEqual({ woman: "aria", man: "atlas" });
});

test("Remove sends null; a new token sends the token", async () => {
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-tts-token-remove"));
  expect(screen.getByTestId("hq-calls-tts-token-state")).toHaveTextContent("Removed on save");
  await userEvent.type(screen.getByTestId("hq-calls-stt-token"), "sk-new");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.tts?.api_key).toBeNull();
  expect(patch.stt?.api_key).toBe("sk-new");
});

test("the server's refusal is shown", async () => {
  vi.mocked(api.saveVoiceSettings).mockRejectedValueOnce(
    new ApiError(422, "tts: enter the API key for https://elsewhere.example:443"),
  );
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-save"));
  expect(await screen.findByTestId("hq-calls-save-msg")).toHaveTextContent(
    "enter the API key for https://elsewhere.example:443",
  );
});

test("Test both checks the typed endpoints with a voice and lists every check", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "tts.synthesize", ok: true, required: true, note: "", value: null },
      { id: "tts.streaming", ok: false, required: false, note: "no streaming: first audio waits", value: null },
    ],
  });
  renderPage();
  const url = await screen.findByTestId("hq-calls-stt-url");
  await userEvent.clear(url);
  await userEvent.type(url, "http://other.lan:9000/v1");
  await userEvent.click(screen.getByTestId("hq-calls-test"));
  expect(api.testVoice).toHaveBeenCalledWith({
    stt: { base_url: "http://other.lan:9000/v1", model: "whisper-1" },
    tts: { base_url: "http://gpu-1.lan:8880/v1", model: "tts" },
    voice: "aria",
  });
  const report = await screen.findByTestId("hq-calls-report");
  expect(report).toHaveAttribute("data-usable", "true");
  expect(within(report).getAllByTestId("hq-calls-check")).toHaveLength(2);
  expect(report).toHaveTextContent("no streaming: first audio waits");
  expect(api.saveVoiceSettings).not.toHaveBeenCalled();
});

test("a conversation setting saves at once", async () => {
  renderPage();
  const seg = await screen.findByTestId("hq-calls-turn-taking");
  await userEvent.click(within(seg).getByRole("radio", { name: "Patient" }));
  await waitFor(() => expect(api.saveVoiceSettings).toHaveBeenCalledWith({ turn_taking: "patient" }));
  await userEvent.click(screen.getByTestId("hq-calls-interrupt"));
  expect(api.saveVoiceSettings).toHaveBeenCalledWith({ interrupt: false });
});

test("push to talk is offered but not selectable yet", async () => {
  renderPage();
  const seg = await screen.findByTestId("hq-calls-mic");
  expect(within(seg).getByRole("radio", { name: "Push to talk" })).toBeDisabled();
});

test("where your call goes comes from the server's view of the URLs", async () => {
  renderPage();
  const dest = await screen.findByTestId("hq-calls-destinations");
  await waitFor(() => expect(dest).toHaveTextContent("gpu-1.lan:8000"));
  expect(dest).toHaveTextContent("ai.example.com (your AI endpoint)");
  expect(dest).toHaveTextContent("Remote");
  expect(dest).toHaveTextContent("Never audio.");
});

test("a save records the language capabilities from Test both of THESE endpoints (#1397 P3)", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "stt.language", ok: true, required: false, note: "", value: { verdict: "detects" } },
      { id: "tts.language", ok: false, required: false, note: "refused the field", value: null },
    ],
  });
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-test"));
  await screen.findByTestId("hq-calls-report");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.stt_detects_language).toBe(true);
  expect(patch.tts_accepts_language).toBe(false);
});

test("a language check that couldn't run is saved untested, never as 'can't detect' (web review of #1640)", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "stt.language", ok: false, required: false, note: "could not be checked",
        value: { verdict: "unknown" } },
      { id: "tts.language", ok: false, required: false, note: "", value: null },
    ],
  });
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-test"));
  await screen.findByTestId("hq-calls-report");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.stt_detects_language).toBeNull();
});

test("a proven 'no language' verdict is saved as false", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "stt.language", ok: false, required: false, note: "", value: { verdict: "no_language" } },
    ],
  });
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-test"));
  await screen.findByTestId("hq-calls-report");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  expect(vi.mocked(api.saveVoiceSettings).mock.calls[0][0].stt_detects_language).toBe(false);
});

test("a test made with another token than the one saved doesn't count", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "stt.language", ok: true, required: false, note: "", value: { verdict: "detects" } },
      { id: "tts.language", ok: true, required: false, note: "", value: null },
    ],
  });
  renderPage();
  const token = await screen.findByTestId("hq-calls-stt-token");
  await userEvent.type(token, "sk-typed-for-the-test");
  await userEvent.click(screen.getByTestId("hq-calls-test"));
  await screen.findByTestId("hq-calls-report");
  await userEvent.clear(token); // the token tested with is not the one being saved
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.stt_detects_language).not.toBe(true);
  expect(patch.tts_accepts_language).not.toBe(true);
});

test("a changed TTS endpoint saved without a test turns its language field off, and only that (#1640 review 6912)", async () => {
  renderPage();
  const url = await screen.findByTestId("hq-calls-tts-url");
  await userEvent.clear(url);
  await userEvent.type(url, "http://strict.example/v1");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.tts_accepts_language).toBe(false); // untested: no `language` sent to it
  // The STT endpoint and its last test are unchanged: what that test proved stands.
  expect(patch).not.toHaveProperty("stt_detects_language");
  // Nothing was read from the new server either: its lists start empty.
  expect(patch.tts_voices).toEqual([]);
  expect(patch.tts_styles).toEqual([]);
});

test("a changed STT endpoint saved without a test leaves detection untested, and only that (#1640 review 6912)", async () => {
  renderPage();
  const url = await screen.findByTestId("hq-calls-stt-url");
  await userEvent.clear(url);
  await userEvent.type(url, "http://other-stt.example/v1");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.stt_detects_language).toBeNull(); // untested: spoken "confirm" refused (fail closed)
  expect(patch).not.toHaveProperty("tts_accepts_language");
  expect(patch).not.toHaveProperty("tts_voices"); // the TTS lists stand too
});

test("a test of other endpoints than the ones saved doesn't count", async () => {
  vi.mocked(api.testVoice).mockResolvedValue({
    usable: true,
    checks: [
      { id: "stt.language", ok: true, required: false, note: "", value: { verdict: "detects" } },
      { id: "tts.language", ok: true, required: false, note: "", value: null },
    ],
  });
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-test"));
  await screen.findByTestId("hq-calls-report");
  const url = screen.getByTestId("hq-calls-tts-url");
  await userEvent.type(url, "x");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.tts_accepts_language).toBe(false);
});

test("Read list records the server's voices, styles and languages on save", async () => {
  vi.mocked(api.voiceLists).mockResolvedValue({
    models: ["tts"],
    voices: ["aria", "atlas", "nova"],
    styles: ["calm", "dramatic"],
    languages: ["en", "de"],
  });
  renderPage();
  await userEvent.click(await screen.findByTestId("hq-calls-tts-read"));
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const patch = vi.mocked(api.saveVoiceSettings).mock.calls[0][0];
  expect(patch.tts_voices).toEqual(["aria", "atlas", "nova"]);
  expect(patch.tts_styles).toEqual(["calm", "dramatic"]);
  expect(patch.tts_languages).toEqual(["en", "de"]);
});

test("the runtime note shows when the server sends one (#1397 P4)", async () => {
  vi.mocked(api.voiceCapabilities).mockResolvedValue({
    ...CAPS,
    runtime_note: "the call runtime is overridden (AGENT_SESSIONS_VOICE_PYTHON)",
  });
  renderPage();
  expect(await screen.findByTestId("hq-calls-runtime-note")).toHaveTextContent("overridden");
});

test("a conversation setting whose save fails shows what is saved again (#1640 review)", async () => {
  renderPage();
  const captions = await screen.findByTestId("hq-calls-captions");
  expect(captions).toBeChecked();
  vi.mocked(api.saveVoiceSettings).mockRejectedValueOnce(new Error("offline"));
  await userEvent.click(captions);
  await waitFor(() => expect(captions).toBeChecked()); // back to the persisted On
});

/** A fake server holding the settings block: each save merges into it (contacts per id) and
 *  answers the WHOLE block, when the test lets it. */
function storedServer() {
  const state = { ...SETTINGS, contacts: {} as Record<string, unknown> } as typeof SETTINGS & {
    contacts: Record<string, unknown>;
  };
  const pending: { fail: (e: Error) => void; ok: () => void }[] = [];
  vi.mocked(api.voiceSettings).mockImplementation(async () => structuredClone(state));
  vi.mocked(api.saveVoiceSettings).mockImplementation(
    (patch: Record<string, unknown>) =>
      new Promise((resolve, reject) => {
        pending.push({
          ok: () => {
            const { contacts, ...rest } = patch as { contacts?: Record<string, unknown> };
            Object.assign(state, rest);
            if (contacts) state.contacts = { ...state.contacts, ...contacts };
            resolve(structuredClone(state));
          },
          fail: (e) => reject(e),
        });
      }) as never,
  );
  return { state, pending };
}

test("conversation saves run one after another: the stored value is the last choice (#1640)", async () => {
  renderPage();
  const captions = await screen.findByTestId("hq-calls-captions");
  const server = storedServer();
  await userEvent.click(captions); // off
  await userEvent.click(captions); // on again
  expect(server.pending).toHaveLength(1); // the second waits for the first to answer
  await act(async () => server.pending[0].ok());
  await waitFor(() => expect(server.pending).toHaveLength(2));
  await act(async () => server.pending[1].ok());
  expect(server.state.captions).toBe(true);
  expect(captions).toBeChecked();
});

/** Settle the NEWEST unsettled request first, until none is left: with writes in flight at once
 *  that reverses their order; with a queue there is only ever one. */
async function settleNewestFirst(pending: { settle: () => void; done: boolean }[]) {
  for (let i = 0; i < 20; i++) {
    const open = pending.filter((p) => !p.done);
    if (!open.length) {
      await act(async () => new Promise((r) => setTimeout(r, 20))); // a queued follow-up?
      if (!pending.some((p) => !p.done)) return;
      continue;
    }
    const last = open[open.length - 1];
    last.done = true;
    await act(async () => last.settle());
  }
}

test("two failed saves end showing what the server actually holds (#1640)", async () => {
  renderPage();
  const captions = await screen.findByTestId("hq-calls-captions");
  const pending: { settle: () => void; done: boolean }[] = [];
  vi.mocked(api.voiceSettings).mockImplementation(async () => structuredClone(SETTINGS)); // On
  vi.mocked(api.saveVoiceSettings).mockImplementation(
    () =>
      new Promise((_, reject) =>
        pending.push({ settle: () => reject(new Error("offline")), done: false }),
      ) as never,
  );
  await userEvent.click(captions); // Off
  await userEvent.click(captions); // On
  await settleNewestFirst(pending); // every save fails
  await act(async () => new Promise((r) => setTimeout(r, 30))); // the queued re-reads
  expect(captions).toBeChecked(); // what the server holds — never a value it does not
});

test("saves of two Generals both stay, whatever order their answers come in (#1640)", async () => {
  renderPage();
  await screen.findByTestId("hq-calls-captions");
  // The server COMMITS each save when it arrives and answers the block as it was then.
  const state = { ...SETTINGS, contacts: {} as Record<string, unknown> };
  const pending: { settle: () => void; done: boolean }[] = [];
  vi.mocked(api.voiceSettings).mockImplementation(async () => structuredClone(state));
  vi.mocked(api.saveVoiceSettings).mockImplementation(
    (patch: { contacts?: Record<string, unknown> }) => {
      state.contacts = { ...state.contacts, ...(patch.contacts ?? {}) };
      const snapshot = structuredClone(state);
      return new Promise((resolve) =>
        pending.push({ settle: () => resolve(snapshot), done: false }),
      ) as never;
    },
  );
  const speeds = screen.getAllByTestId("hq-calls-contact-speed");
  await userEvent.selectOptions(speeds[0], "1.1");
  await userEvent.selectOptions(speeds[1], "0.9");
  await settleNewestFirst(pending);
  expect(Object.keys(state.contacts)).toHaveLength(2);
  await waitFor(() => expect(screen.getAllByTestId("hq-calls-contact-speed")[1]).toHaveValue("0.9"));
  expect(screen.getAllByTestId("hq-calls-contact-speed")[0]).toHaveValue("1.1");
});

test("while a newer choice waits in the queue, an earlier answer never shows the older value (#1640)", async () => {
  renderPage();
  const captions = await screen.findByTestId("hq-calls-captions");
  const server = storedServer(); // holds captions: true
  await userEvent.click(captions); // Off
  await userEvent.click(captions); // On — queued behind the Off
  await act(async () => server.pending[0].ok()); // the Off answers: stored Off, for now
  expect(captions).toBeChecked(); // the queued On stays on screen
  await waitFor(() => expect(server.pending).toHaveLength(2));
  await act(async () => server.pending[1].ok());
  expect(captions).toBeChecked();
  expect(server.state.captions).toBe(true);
});

test("a General's save answering never reverts a conversation choice made after it (#1640)", async () => {
  renderPage();
  const captions = await screen.findByTestId("hq-calls-captions");
  const server = storedServer(); // holds captions: true
  await userEvent.selectOptions(screen.getAllByTestId("hq-calls-contact-speed")[0], "1.1");
  await userEvent.click(captions); // Off — made after the General's save was queued
  await act(async () => server.pending[0].ok()); // the General answers (captions still On there)
  expect(captions).not.toBeChecked(); // the newer Off stays
  await waitFor(() => expect(server.pending).toHaveLength(2));
  await act(async () => server.pending[1].ok());
  expect(server.state.captions).toBe(false);
  expect(captions).not.toBeChecked();
});

test("edits made while a save is in flight stay when its answer lands (#1640 review 6907)", async () => {
  let answer!: (s: VoiceSettings) => void;
  vi.mocked(api.saveVoiceSettings).mockImplementationOnce(() => new Promise((r) => (answer = r)));
  renderPage();
  await userEvent.type(await screen.findByTestId("hq-calls-stt-token"), "sk-sent");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  // Typed after Save, before the server answered.
  const url = screen.getByTestId("hq-calls-stt-url");
  await userEvent.clear(url);
  await userEvent.type(url, "http://gpu-2.lan:8000/v1");
  await userEvent.clear(screen.getByTestId("hq-calls-voice-woman"));
  await userEvent.type(screen.getByTestId("hq-calls-voice-woman"), "nova");
  // The stored block, as the server normalised it.
  await act(async () =>
    answer({
      ...SETTINGS,
      stt: { ...SETTINGS.stt, api_key_set: true },
      tts: { ...SETTINGS.tts, model: "tts-1" },
    }),
  );
  expect(screen.getByTestId("hq-calls-stt-url")).toHaveValue("http://gpu-2.lan:8000/v1"); // edited: kept
  expect(screen.getByTestId("hq-calls-voice-woman")).toHaveValue("nova"); // edited: kept
  expect(screen.getByTestId("hq-calls-tts-model")).toHaveValue("tts-1"); // untouched: what is stored
  // The token that was SENT is cleared (it is stored now and never shown again)…
  expect(screen.queryByDisplayValue("sk-sent")).toBeNull();
});

test("a token typed while its save is in flight is kept, not cleared (#1640 review 6907)", async () => {
  let answer!: (s: VoiceSettings) => void;
  vi.mocked(api.saveVoiceSettings).mockImplementationOnce(() => new Promise((r) => (answer = r)));
  renderPage();
  await userEvent.type(await screen.findByTestId("hq-calls-stt-token"), "sk-first");
  await userEvent.click(screen.getByTestId("hq-calls-save"));
  const tok = screen.getByTestId("hq-calls-stt-token");
  await userEvent.clear(tok);
  await userEvent.type(tok, "sk-second");
  await act(async () => answer({ ...SETTINGS, stt: { ...SETTINGS.stt, api_key_set: true } }));
  expect(screen.getByDisplayValue("sk-second")).toBeInTheDocument();
});
