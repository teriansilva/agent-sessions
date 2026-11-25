/** Speech on this machine (#1397 P4): the four states, the literal command on the clipboard, and
 *  Use this machine's three answers. The access key is never asked for or shown. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { VoiceLocal, VoiceSettings } from "../types/voice";
import { HqCallsLocal } from "./HqCallsLocal";

/** The page's write queue, as the settings page runs it: one after another. */
let tail: Promise<unknown> = Promise.resolve();
const inOrder = <T,>(fn: () => Promise<T>): Promise<T> => {
  const run = tail.then(fn, fn);
  tail = run.catch(() => undefined);
  return run;
};

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { voiceLocal: vi.fn(), useLocalVoice: vi.fn(), testVoice: vi.fn() } };
});

const BASE: VoiceLocal = {
  installed: { runtime: true, server: true },
  token: true,
  unit: true,
  systemd: true,
  stt: "small",
  last_error: null,
  stale: null,
  url: "http://127.0.0.1:8766/v1",
  running: true,
  in_use: false,
  voices: { woman: "af_heart", man: "am_michael" },
  limits: "hard",
};
const REPORT = { usable: true, checks: [{ id: "stt.language", ok: true, required: false, note: "", value: null }] };

const onUsed = vi.fn();
beforeEach(() => vi.clearAllMocks());

async function show(over: Partial<VoiceLocal>) {
  vi.mocked(api.voiceLocal).mockResolvedValue({ ...BASE, ...over });
  render(<HqCallsLocal
      switchToLocal={() =>
        inOrder(() => api.useLocalVoice()).then((g) => {
          onUsed(g.settings);
          return g;
        })
      }
    />);
  return await screen.findByTestId("hq-calls-local");
}

test("not set up: the setup command, about 700 MB", async () => {
  const el = await show({ installed: { runtime: false, server: false }, token: false, running: false });
  expect(el).toHaveAttribute("data-state", "not_set_up");
  expect(screen.getByTestId("hq-calls-local-cmd-text")).toHaveTextContent("agent-sessions voice setup");
  expect(el).toHaveTextContent("Downloads about 700 MB after you confirm. Checksums are verified; nothing starts until it's done.");
  expect(screen.queryByTestId("hq-calls-local-use")).toBeNull();
});

test("a server without its token is not set up", async () => {
  expect(await show({ token: false, running: false })).toHaveAttribute("data-state", "not_set_up");
});

test("setup failed names the error and offers the command again", async () => {
  const el = await show({ installed: { runtime: false, server: false }, token: false, running: false, last_error: "the voice model didn't match its checksum" });
  expect(el).toHaveAttribute("data-state", "failed");
  expect(screen.getByTestId("hq-calls-local-error")).toHaveTextContent("the voice model didn't match its checksum");
  expect(screen.getByTestId("hq-calls-local-cmd-text")).toHaveTextContent("agent-sessions voice setup");
});

test("stopped: the user service's start command, else voice serve", async () => {
  const el = await show({ running: false });
  expect(el).toHaveAttribute("data-state", "stopped");
  expect(screen.getByTestId("hq-calls-local-cmd-text")).toHaveTextContent("systemctl --user start agent-sessions-speech");
});

test("stopped without a user service manager: voice serve", async () => {
  await show({ running: false, systemd: false, unit: null });
  expect(screen.getByTestId("hq-calls-local-cmd-text")).toHaveTextContent("agent-sessions voice serve");
});

test("Copy puts the literal command on the clipboard", async () => {
  const user = userEvent.setup();
  await show({ installed: { runtime: false, server: false }, token: false, running: false });
  await user.click(screen.getByRole("button", { name: "Copy the setup command" }));
  expect(await navigator.clipboard.readText()).toBe("agent-sessions voice setup");
  expect(screen.getByTestId("hq-calls-local-cmd-copy")).toHaveTextContent("Copied");
});

test("running: Use this machine; In use adds Test both; soft limits say so", async () => {
  const el = await show({ in_use: true, limits: "soft" });
  expect(el).toHaveAttribute("data-state", "running");
  expect(screen.getByTestId("hq-calls-local-in-use")).toHaveTextContent("In use");
  expect(screen.getByTestId("hq-calls-local-test")).toHaveTextContent("Test both");
  expect(screen.getByTestId("hq-calls-local-soft")).toHaveTextContent(
    "Soft limits only: no memory or CPU cap without the user service.",
  );
});

test("hard limits: no soft hint, and Test both waits until it is in use", async () => {
  await show({});
  expect(screen.queryByTestId("hq-calls-local-soft")).toBeNull();
  expect(screen.queryByTestId("hq-calls-local-test")).toBeNull();
  expect(document.body.textContent).not.toMatch(/token|SPEECH_SERVER/i);
});

test("Use this machine: 200 hands the saved settings up and shows the check", async () => {
  const saved = { configured: true } as VoiceSettings;
  vi.mocked(api.useLocalVoice).mockResolvedValue({ settings: saved, report: REPORT });
  await show({});
  await userEvent.click(screen.getByTestId("hq-calls-local-use"));
  expect(onUsed).toHaveBeenCalledWith(saved);
  expect(await screen.findByTestId("hq-calls-local-report")).toHaveAttribute("data-usable", "true");
  expect(api.useLocalVoice).toHaveBeenCalledWith();
});

test("Use this machine: a 409 says why; a 502 shows the failed check", async () => {
  vi.mocked(api.useLocalVoice).mockRejectedValueOnce(
    new ApiError(409, "the local speech server isn't running", { detail: "the local speech server isn't running", code: "not_running" }),
  );
  await show({});
  await userEvent.click(screen.getByTestId("hq-calls-local-use"));
  expect(await screen.findByTestId("hq-calls-local-msg")).toHaveTextContent("the local speech server isn't running");
  expect(onUsed).not.toHaveBeenCalled();
  vi.mocked(api.useLocalVoice).mockRejectedValueOnce(
    new ApiError(502, "the local speech server failed the check", {
      detail: "the local speech server failed the check",
      report: { usable: false, checks: [{ id: "tts.synthesize", ok: false, required: true, note: "no audio", value: null }] },
    }),
  );
  await userEvent.click(screen.getByTestId("hq-calls-local-use"));
  expect(await screen.findByTestId("hq-calls-local-report")).toHaveAttribute("data-usable", "false");
  expect(screen.getByTestId("hq-calls-local-msg")).toHaveTextContent("failed the check");
});

test("running: the speech model is the one setup installed, never assumed", async () => {
  await show({ stt: "base" });
  expect(screen.getByText("faster-whisper base")).toBeInTheDocument();
  expect(document.body.textContent).not.toMatch(/faster-whisper small/);
});
