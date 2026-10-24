/** Settings → AI → HQ calls (#1397 P1, P3, P4).
 *
 *  Speech is configured like the HQ model: speech-to-text and text-to-speech are each an
 *  OpenAI-compatible endpoint — a URL, an OPTIONAL token (a self-hosted server usually has none)
 *  and a model. Tokens never come back from the server: a stored one shows as "Saved" (never a
 *  suffix), a blank field keeps it, Remove clears it. **Test both** runs the compatibility check on
 *  what is typed, without saving. Voices, how a call feels, and where the call's audio goes.
 *
 *  What the speech server can do is discovered, never assumed (#1397 P3): saving records the voice,
 *  style and language lists from the last **Read list** of THIS text-to-speech endpoint, and the
 *  language capabilities from the last **Test both** of THESE endpoints. Endpoints that changed
 *  without a test are saved with both language capabilities off (fail closed: no `language` is
 *  sent to a server that was never shown to accept it). */
import { Play } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

import { useCall } from "../components/call/callContext";
import { Seg, Switch } from "../components/hud/Controls";
import { api, ApiError } from "../lib/api";
import type {
  AnswerLength,
  ConformanceReport,
  HostKind,
  MicMode,
  SpeechSidePatch,
  TurnTaking,
  VoiceCapabilities,
  VoiceGroup,
  VoiceLists,
  VoiceSettings,
  VoiceSettingsPatch,
} from "../types/voice";
import { playWav } from "./hqCallsAudio";
import { HqCallsLocal } from "./HqCallsLocal";
import { HqCallsProfiles } from "./HqCallsProfiles";
import { ConformanceReportView } from "./HqCallsReport";
import styles from "./Settings.module.css";
import h from "./HqCallsSettings.module.css";

type Side = "stt" | "tts";

/** The endpoints form as a write was sent: what a stored answer may replace. */
interface Form {
  draft: Record<Side, SideDraft>;
  voices: Record<VoiceGroup, string>;
}

interface SideDraft {
  base_url: string;
  model: string;
  /** A new token typed here (blank = keep the stored one). */
  token: string;
  /** Remove the stored token on save. */
  remove: boolean;
}

const SIDE_LABEL: Record<Side, string> = { stt: "Speech-to-text", tts: "Text-to-speech" };
const SIDE_WHAT: Record<Side, string> = {
  stt: "where your voice is transcribed",
  tts: "where HQ's voice is made",
};
const KIND_WORD: Record<HostKind, string> = {
  loopback: "This machine",
  lan: "LAN",
  remote: "Remote",
  invalid: "Not a valid URL",
};

type ConvKey = "turn_taking" | "interrupt" | "mic" | "answers" | "captions";
/** Runs one settings write after every earlier one (the page's single write queue). */
export type SettingsWrite = <T>(fn: () => Promise<T>) => Promise<T>;
const CONV_KEYS: ConvKey[] = ["turn_taking", "interrupt", "mic", "answers", "captions"];

/** The endpoint a draft points at — and the credential it would use: a test or a read is only
 *  valid for exactly that (a server may answer one token differently from another). */
const sig = (...sides: SideDraft[]) =>
  JSON.stringify(sides.map((d) => [d.base_url.trim(), d.model.trim(), d.token, d.remove]));
/** The STT language verdict the server proved — never inferred from a check that couldn't run:
 *  `detects` → true, `no_language` → false, anything else → null (untested; "confirm" stays off). */
const detection = (report: ConformanceReport): boolean | null => {
  const check = report.checks.find((c) => c.id === "stt.language");
  const verdict = (check?.value as { verdict?: unknown } | null | undefined)?.verdict;
  return verdict === "detects" ? true : verdict === "no_language" ? false : null;
};
/** A discovered list as the server stores it: short strings, at most 200. */
const storable = (xs: string[] | null | undefined) =>
  (xs ?? []).filter((x) => typeof x === "string" && x.length > 0 && x.length <= 40).slice(0, 200);

function sideDraft(s: VoiceSettings[Side]): SideDraft {
  return { base_url: s.base_url, model: s.model, token: "", remove: false };
}

/** The edit form after a stored answer: each field takes the stored value unless it was edited
 *  AFTER the write was sent (`sent`: the form then) — an edit made while the save was in flight
 *  stays, a token typed meanwhile included (#1640 review 6907). */
function settleDraft(cur: SideDraft, sent: SideDraft, stored: SideDraft): SideDraft {
  const pick = <K extends keyof SideDraft>(k: K): SideDraft[K] => (cur[k] !== sent[k] ? cur[k] : stored[k]);
  return { base_url: pick("base_url"), model: pick("model"), token: pick("token"), remove: pick("remove") };
}

function sidePatch(d: SideDraft): SpeechSidePatch {
  const out: SpeechSidePatch = { base_url: d.base_url.trim(), model: d.model.trim() };
  if (d.remove) out.api_key = null;
  else if (d.token.trim()) out.api_key = d.token.trim();
  return out;
}

function TokenField({
  side,
  set,
  draft,
  onChange,
}: {
  side: Side;
  set: boolean;
  draft: SideDraft;
  onChange: (d: SideDraft) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  // A typed token not saved yet keeps the field open, e.g. one typed while an earlier save was
  // in flight and that save stored a token (#1640 review 6907).
  const replacing = editing || draft.token !== "";
  const showInput = !set || replacing || draft.remove;
  return (
    <div className={styles.aiField}>
      <label className={styles.aiFieldLabel} {...(showInput ? { htmlFor: id } : {})}>
        API token <span className={h.optional}>optional</span>
      </label>
      <div className={styles.aiKeyRow}>
        {showInput && !draft.remove ? (
          <input
            id={id}
            className={styles.aiInput}
            type="password"
            autoComplete="new-password"
            data-1p-ignore="true"
            data-lpignore="true"
            data-bwignore="true"
            data-form-type="other"
            spellCheck={false}
            placeholder={set ? "new token" : "none — not needed by most self-hosted servers"}
            value={draft.token}
            onChange={(e) => onChange({ ...draft, token: e.target.value })}
            data-testid={`hq-calls-${side}-token`}
          />
        ) : draft.remove ? (
          <span className={styles.aiSecretReadout} data-testid={`hq-calls-${side}-token-state`}>
            Removed on save
          </span>
        ) : (
          <span className={styles.aiSecretReadout} data-testid={`hq-calls-${side}-token-state`}>
            Saved
          </span>
        )}
        {set && !replacing && !draft.remove ? (
          <>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => setEditing(true)}
              data-testid={`hq-calls-${side}-token-replace`}
            >
              Replace
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => onChange({ ...draft, token: "", remove: true })}
              data-testid={`hq-calls-${side}-token-remove`}
            >
              Remove
            </button>
          </>
        ) : set ? (
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setEditing(false);
              onChange({ ...draft, token: "", remove: false });
            }}
          >
            Keep saved token
          </button>
        ) : null}
      </div>
      <p className={styles.hint}>Sent only to this endpoint, and never shown again once saved.</p>
    </div>
  );
}

function SideFields({
  side,
  settings,
  draft,
  onChange,
  lists,
  onRead,
  reading,
}: {
  side: Side;
  settings: VoiceSettings;
  draft: SideDraft;
  onChange: (d: SideDraft) => void;
  lists: { models: string[] | null; voices: string[] | null } | null;
  onRead: () => void;
  reading: boolean;
}) {
  const id = useId();
  return (
    <fieldset className={h.side} data-testid={`hq-calls-${side}`}>
      <legend className={h.legend}>
        {SIDE_LABEL[side]} <span className={h.what}>— {SIDE_WHAT[side]}</span>
      </legend>
      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${id}-url`}>
          Endpoint (OpenAI-compatible)
        </label>
        <input
          id={`${id}-url`}
          className={styles.aiInput}
          type="url"
          spellCheck={false}
          placeholder={side === "stt" ? "http://gpu-1.lan:8000/v1" : "http://gpu-1.lan:8880/v1"}
          value={draft.base_url}
          onChange={(e) => onChange({ ...draft, base_url: e.target.value })}
          data-testid={`hq-calls-${side}-url`}
        />
      </div>
      <TokenField side={side} set={settings[side].api_key_set} draft={draft} onChange={onChange} />
      <div className={styles.aiField}>
        <label className={styles.aiFieldLabel} htmlFor={`${id}-model`}>
          Model
        </label>
        <div className={styles.aiKeyRow}>
          <input
            id={`${id}-model`}
            className={styles.aiInput}
            list={`${id}-models`}
            spellCheck={false}
            placeholder={side === "stt" ? "whisper-1" : "tts"}
            value={draft.model}
            onChange={(e) => onChange({ ...draft, model: e.target.value })}
            data-testid={`hq-calls-${side}-model`}
          />
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={onRead}
            disabled={reading || !draft.base_url.trim()}
            data-testid={`hq-calls-${side}-read`}
          >
            {reading ? "Reading…" : "Read list"}
          </button>
        </div>
        <datalist id={`${id}-models`}>
          {(lists?.models ?? []).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        {lists && lists.models === null ? (
          <p className={styles.hint}>This endpoint publishes no model list: type the model.</p>
        ) : null}
      </div>
    </fieldset>
  );
}

export function HqCallsSettings() {
  const [settings, setSettings] = useState<VoiceSettings | null>(null);
  const convSeq = useRef<Partial<Record<ConvKey, number>>>({});
  // ONE write queue for the whole page (#1640 reviews): conversation settings, the endpoints,
  // the call language, each General's profile and Use this machine all write the same settings
  // block, and each answer is the WHOLE block. Run one at a time, the server commits them in
  // request order and their answers arrive in that order, so the last answer applied is always
  // what is stored — no late answer can hide another write's result.
  const writes = useRef<Promise<unknown>>(Promise.resolve());
  const write: SettingsWrite = useCallback(<T,>(fn: () => Promise<T>): Promise<T> => {
    const run = writes.current.then(fn, fn);
    writes.current = run.catch(() => undefined);
    return run;
  }, []);
  const [caps, setCaps] = useState<VoiceCapabilities | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<Side, SideDraft> | null>(null);
  const [voices, setVoices] = useState<Record<VoiceGroup, string>>({ woman: "", man: "" });
  const [lists, setLists] = useState<Record<Side, VoiceLists | null>>({ stt: null, tts: null });
  /** Which TTS endpoint `lists.tts` was read from, and which pair `report` tested. */
  const [listsSig, setListsSig] = useState<string | null>(null);
  const [reportSig, setReportSig] = useState<string | null>(null);
  const [reading, setReading] = useState<Side | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [report, setReport] = useState<ConformanceReport | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState<VoiceGroup | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const voicesId = useId();

  const adopt = useCallback((s: VoiceSettings) => {
    setSettings(s);
    setDraft({ stt: sideDraft(s.stt), tts: sideDraft(s.tts) });
    setVoices({ ...s.voices });
  }, []);
  // The shell's Call buttons read the same capabilities from the call provider: every stored
  // change refreshes both, so Dashboard offers Call (or Set up calls) at once, not after a
  // reload or a tab switch (#1640 review 6912).
  const { refreshCaps: refreshShellCaps } = useCall();
  const capsGen = useRef(0);
  const refreshCaps = useCallback(() => {
    const gen = ++capsGen.current; // the newest read wins here too (#1640 review 6916)
    api
      .voiceCapabilities()
      .then((c) => {
        if (gen === capsGen.current) setCaps(c);
      })
      .catch(() => undefined);
    refreshShellCaps();
  }, [refreshShellCaps]);
  useEffect(() => {
    api
      .voiceSettings()
      .then(adopt)
      .catch(() => setLoadError("Couldn’t read the call settings — reload to try again."));
    refreshCaps();
  }, [adopt, refreshCaps]);

  if (loadError) {
    return (
      <p className={styles.err} role="alert">
        {loadError}
      </p>
    );
  }
  if (!settings || !draft) return null;

  const saveFields = async () => {
    setSaving(true);
    setSaveMsg(null);
    const savedSides = { stt: sideDraft(settings.stt), tts: sideDraft(settings.tts) };
    const extra: VoiceSettingsPatch = {};
    if (report && reportSig === sig(draft.stt, draft.tts)) {
      const ok = (id: string) => report.checks.some((c) => c.id === id && c.ok);
      extra.stt_detects_language = detection(report);
      extra.tts_accepts_language = ok("tts.language");
    } else {
      // Untested: only the side that changed loses what its last test proved (#1640 review
      // 6912) — a new TTS server says nothing about whether the STT one detects languages.
      if (sig(draft.stt) !== sig(savedSides.stt)) extra.stt_detects_language = null; // "confirm" stays off
      if (sig(draft.tts) !== sig(savedSides.tts)) extra.tts_accepts_language = false;
    }
    if (lists.tts && listsSig === sig(draft.tts)) {
      extra.tts_voices = storable(lists.tts.voices);
      extra.tts_styles = storable(lists.tts.styles);
      extra.tts_languages = storable(lists.tts.languages);
    } else if (sig(draft.tts) !== sig(savedSides.tts)) {
      extra.tts_voices = [];
      extra.tts_styles = [];
      extra.tts_languages = [];
    }
    try {
      await writeBlock(
        () =>
          api.saveVoiceSettings({
            stt: sidePatch(draft.stt),
            tts: sidePatch(draft.tts),
            voices,
            ...extra,
          }),
        { draft, voices },
      );
      setSaveMsg({ ok: true, text: "Saved." });
    } catch (e) {
      setSaveMsg({
        ok: false,
        text: e instanceof ApiError && e.message ? e.message : "Couldn’t save — please try again.",
      });
    } finally {
      setSaving(false);
    }
  };

  /** A conversation setting saves at once (it has no draft) and shows at once. Writes go through
   *  the page's queue, so the server stores the last choice made; while a newer save of the same
   *  setting is queued, an earlier answer never shows its older value. A failed save is followed
   *  by a read of what the server ACTUALLY holds (queued behind every write), never a guess. */
  const saveOne = async (patch: Partial<Pick<VoiceSettings, ConvKey>>) => {
    const keys = Object.keys(patch) as ConvKey[];
    for (const k of keys) convSeq.current[k] = (convSeq.current[k] ?? 0) + 1;
    setSettings((cur) => (cur ? { ...cur, ...patch } : cur));
    try {
      await writeBlock(() => api.saveVoiceSettings(patch));
    } catch (e) {
      setSaveMsg({
        ok: false,
        text: e instanceof ApiError && e.message ? e.message : "Couldn’t save — please try again.",
      });
      const at = { ...convSeq.current };
      void write(() => api.voiceSettings())
        .then((stored) => showStored(stored, at))
        .catch(() => undefined);
    }
  };
  /** A whole stored block, except the conversation choices made AFTER its write was queued
   *  (`at`: the choices' sequence numbers then) — those are newer, and their own writes follow.
   *  `whole`: also take the stored endpoints into the drafts (a save of the endpoints themselves,
   *  or Use this machine), given as the form was when the write was sent: a field edited since
   *  keeps the edit; `"all"` takes every field. Otherwise the drafts being edited stay. */
  const showStored = (stored: VoiceSettings, at: Partial<Record<ConvKey, number>>, whole?: Form | "all") => {
    if (whole) {
      const sent = whole === "all" ? null : whole;
      setDraft((cur) =>
        cur && sent
          ? {
              stt: settleDraft(cur.stt, sent.draft.stt, sideDraft(stored.stt)),
              tts: settleDraft(cur.tts, sent.draft.tts, sideDraft(stored.tts)),
            }
          : { stt: sideDraft(stored.stt), tts: sideDraft(stored.tts) },
      );
      setVoices((cur) => ({
        woman: sent && cur.woman !== sent.voices.woman ? cur.woman : stored.voices.woman,
        man: sent && cur.man !== sent.voices.man ? cur.man : stored.voices.man,
      }));
    }
    setSettings((cur) => {
      if (!cur) return stored;
      const newer = CONV_KEYS.filter((k) => convSeq.current[k] !== at[k]);
      const keep = Object.fromEntries(newer.map((k) => [k, cur[k]]));
      return whole
        ? { ...stored, ...keep }
        : { ...cur, ...stored, stt: cur.stt, tts: cur.tts, ...keep };
    });
  };
  /** One write through the page's queue, its answer shown by `showStored`. The conversation
   *  choices' sequence is taken HERE, when the write is queued — not when it answers. */
  const writeBlock = async (fn: () => Promise<VoiceSettings>, whole?: Form | "all") => {
    const at = { ...convSeq.current };
    const next = await write(fn);
    showStored(next, at, whole);
    refreshCaps();
    return next;
  };
  const switchToLocal = async () => {
    const at = { ...convSeq.current };
    const form: Form | "all" = draft ? { draft, voices } : "all";
    const got = await write(() => api.useLocalVoice());
    showStored(got.settings, at, form);
    refreshCaps();
    return got;
  };

  const readLists = async (side: Side) => {
    setReading(side);
    try {
      const got = await api.voiceLists(side, sidePatch(draft[side]));
      setLists((l) => ({ ...l, [side]: got }));
      if (side === "tts") setListsSig(sig(draft.tts));
    } catch {
      setLists((l) => ({ ...l, [side]: { models: null, voices: null } }));
      if (side === "tts") setListsSig(null);
    } finally {
      setReading(null);
    }
  };

  const testBoth = async () => {
    setTesting(true);
    setReport(null);
    setTestError(null);
    try {
      setReport(
        await api.testVoice({
          stt: sidePatch(draft.stt),
          tts: sidePatch(draft.tts),
          voice: voices.woman.trim() || voices.man.trim(),
        }),
      );
      setReportSig(sig(draft.stt, draft.tts));
    } catch (e) {
      setTestError(e instanceof ApiError && e.message ? e.message : "The test could not run.");
    } finally {
      setTesting(false);
    }
  };

  const preview = async (group: VoiceGroup) => {
    setPreviewing(group);
    setPreviewError(null);
    try {
      await playWav(await api.voicePreview(group, voices[group].trim() || undefined));
    } catch (e) {
      setPreviewError(e instanceof ApiError && e.message ? e.message : "The preview could not play.");
    } finally {
      setPreviewing(null);
    }
  };

  const dest = caps?.destinations;
  const voiceList = lists.tts?.voices ?? null;
  // The voices each General may pick from: a fresh read of this endpoint, else the saved list.
  const knownVoices = listsSig === sig(draft.tts) && lists.tts ? storable(lists.tts.voices) : settings.tts_voices;
  const canTest =
    draft.stt.base_url.trim() !== "" && draft.tts.base_url.trim() !== "" && (voices.woman.trim() || voices.man.trim());

  return (
    <div data-testid="hq-calls-settings">
      <div className={styles.endpointStrip} data-testid="hq-calls-status">
        <span>
          Calls <b>{settings.configured ? "set up" : "not set up"}</b>
        </span>
        {caps ? (
          <span className={styles.endpointStripState}>
            <span className={`hud-led ${caps.available ? "up" : "att"}`} aria-hidden="true" />
            {caps.available ? "Ready" : caps.reason || "Unavailable"}
          </span>
        ) : null}
      </div>
      {caps?.runtime_note ? (
        <p className={styles.hint} data-testid="hq-calls-runtime-note">
          <span className="nj-stamp is-warn">Call runtime</span> {caps.runtime_note}
        </p>
      ) : null}

      <HqCallsLocal switchToLocal={switchToLocal} />

      <section className={styles.section} aria-labelledby="hq-calls-endpoints-h">
        <h2 id="hq-calls-endpoints-h">Speech endpoints</h2>
        <p className={styles.hint}>
          Any OpenAI-compatible speech endpoint: OpenAI itself, or your own server (for example
          Speaches with faster-whisper, or Kokoro-FastAPI). Turn-taking and interruptions run in
          BattleLab’s voice worker either way.
        </p>
        {(["stt", "tts"] as const).map((side) => (
          <SideFields
            key={side}
            side={side}
            settings={settings}
            draft={draft[side]}
            onChange={(d) => setDraft({ ...draft, [side]: d })}
            lists={lists[side]}
            onRead={() => void readLists(side)}
            reading={reading === side}
          />
        ))}
        <div className={h.turnRow}>
          <span className={styles.aiFieldLabel}>Turn detection</span>
          <span className={h.fixed}>Silero VAD + Smart Turn · runs in the voice worker</span>
        </div>
        <div className={styles.cardActions}>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => void testBoth()}
            disabled={testing || !canTest}
            data-testid="hq-calls-test"
          >
            {testing ? "Testing…" : "Test both"}
          </button>
        </div>
        {!canTest ? (
          <p className={styles.hint}>Fill in both endpoints and a voice to test them.</p>
        ) : null}
        {testError ? (
          <p className={styles.err} role="alert" data-testid="hq-calls-test-error">
            {testError}
          </p>
        ) : null}
        {report ? <ConformanceReportView report={report} /> : null}
      </section>

      <section className={styles.section} aria-labelledby="hq-calls-voices-h">
        <h2 id="hq-calls-voices-h">Voices</h2>
        <p className={styles.hint}>
          Every General starts with the Woman or Man voice; give any of them their own under Each
          General's voice.
        </p>
        <datalist id={voicesId}>
          {(voiceList ?? []).map((v) => (
            <option key={v} value={v} />
          ))}
        </datalist>
        {(["woman", "man"] as const).map((g) => (
          <div className={styles.aiField} key={g}>
            <label className={styles.aiFieldLabel} htmlFor={`${voicesId}-${g}`}>
              {g === "woman" ? "Woman" : "Man"}
            </label>
            <div className={styles.aiKeyRow}>
              <input
                id={`${voicesId}-${g}`}
                className={styles.aiInput}
                list={voicesId}
                spellCheck={false}
                placeholder={g === "woman" ? "aria" : "atlas"}
                value={voices[g]}
                onChange={(e) => setVoices({ ...voices, [g]: e.target.value })}
                data-testid={`hq-calls-voice-${g}`}
              />
              <button
                type="button"
                className={styles.secBtnGhost}
                onClick={() => void preview(g)}
                disabled={previewing !== null || !settings.tts.base_url}
                data-testid={`hq-calls-preview-${g}`}
              >
                <Play size={14} aria-hidden="true" /> {previewing === g ? "Playing…" : "Preview"}
              </button>
            </div>
          </div>
        ))}
        {lists.tts && voiceList === null ? (
          <p className={styles.hint}>This endpoint publishes no voice list: type the voice.</p>
        ) : null}
        {!settings.tts.base_url ? (
          <p className={styles.hint}>Save a text-to-speech endpoint to preview voices.</p>
        ) : null}
        {previewError ? (
          <p className={styles.err} role="alert">
            {previewError}
          </p>
        ) : null}
      </section>

      <div className={styles.cardFoot}>
        <div className={styles.cardActions}>
          <button
            type="button"
            className={styles.secBtn}
            onClick={() => void saveFields()}
            disabled={saving}
            data-testid="hq-calls-save"
          >
            {saving ? "Saving…" : "Save endpoints and voices"}
          </button>
        </div>
        {saveMsg ? (
          <p
            className={saveMsg.ok ? styles.hint : styles.err}
            role={saveMsg.ok ? "status" : "alert"}
            data-testid="hq-calls-save-msg"
          >
            {saveMsg.text}
          </p>
        ) : null}
      </div>

      <section className={styles.section} aria-labelledby="hq-calls-conv-h">
        <h2 id="hq-calls-conv-h">Conversation</h2>
        <div className={h.row}>
          <div className={h.lab}>
            <b>Turn-taking</b>
            <span>how long HQ waits after you pause</span>
          </div>
          <Seg<TurnTaking>
            label="Turn-taking"
            value={settings.turn_taking}
            onChange={(v) => void saveOne({ turn_taking: v })}
            options={[
              { value: "quick", label: "Quick" },
              { value: "natural", label: "Natural" },
              { value: "patient", label: "Patient" },
            ]}
            testId="hq-calls-turn-taking"
          />
        </div>
        <div className={h.row}>
          <div className={h.lab}>
            <b>Interrupt HQ</b>
            <span>talk over HQ and it stops</span>
          </div>
          <span className={h.switchRow}>
            <Switch
              checked={settings.interrupt}
              onChange={(v) => void saveOne({ interrupt: v })}
              label="Interrupt HQ"
              testId="hq-calls-interrupt"
            />
            {settings.interrupt ? "On" : "Off"}
          </span>
        </div>
        <div className={h.row}>
          <div className={h.lab}>
            <b>Microphone</b>
            <span>push to talk comes in a later release</span>
          </div>
          <Seg<MicMode>
            label="Microphone"
            value={settings.mic}
            onChange={(v) => void saveOne({ mic: v })}
            options={[
              { value: "open", label: "Open mic" },
              { value: "push", label: "Push to talk", disabled: true },
            ]}
            testId="hq-calls-mic"
          />
        </div>
        <div className={h.row}>
          <div className={h.lab}>
            <b>Answers</b>
          </div>
          <Seg<AnswerLength>
            label="Answers"
            value={settings.answers}
            onChange={(v) => void saveOne({ answers: v })}
            options={[
              { value: "brief", label: "Brief" },
              { value: "detailed", label: "Detailed" },
            ]}
            testId="hq-calls-answers"
          />
        </div>
        <div className={h.row}>
          <div className={h.lab}>
            <b>Captions</b>
          </div>
          <span className={h.switchRow}>
            <Switch
              checked={settings.captions}
              onChange={(v) => void saveOne({ captions: v })}
              label="Captions"
              testId="hq-calls-captions"
            />
            {settings.captions ? "On" : "Off"}
          </span>
        </div>
      </section>

      <HqCallsProfiles
        settings={settings}
        saveBlock={(patch) => writeBlock(() => api.saveVoiceSettings(patch))}
        voiceList={knownVoices}
      />

      <section className={styles.section} aria-labelledby="hq-calls-dest-h">
        <h2 id="hq-calls-dest-h">Where your call goes</h2>
        <dl className={h.dest} data-testid="hq-calls-destinations">
          <dt>Your voice</dt>
          <dd>
            <span className={h.host}>{dest?.stt_host || "not set"}</span>
            {dest?.stt ? <span className={`nj-stamp ${dest.stt === "remote" ? "is-warn" : "is-ok"}`}>{KIND_WORD[dest.stt]}</span> : null}
          </dd>
          <dt>HQ’s spoken answers</dt>
          <dd>
            <span className={h.host}>{dest?.tts_host || "not set"}</span>
            {dest?.tts ? <span className={`nj-stamp ${dest.tts === "remote" ? "is-warn" : "is-ok"}`}>{KIND_WORD[dest.tts]}</span> : null}
          </dd>
          <dt>HQ’s questions + fleet context</dt>
          <dd>
            <span className={h.host}>{dest?.ai_host ? `${dest.ai_host} (your AI endpoint)` : "not set"}</span>
            {dest?.ai ? <span className={`nj-stamp ${dest.ai === "remote" ? "is-warn" : "is-ok"}`}>{KIND_WORD[dest.ai]}</span> : null}
          </dd>
          <dt>Kept by BattleLab</dt>
          <dd>The HQ conversation text, like typing. Never audio.</dd>
        </dl>
      </section>
    </div>
  );
}
