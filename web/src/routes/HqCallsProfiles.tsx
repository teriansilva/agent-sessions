/** Settings → AI → HQ calls → Each General's voice, and the call language (#1397 P3).
 *
 *  Every General starts from their own defaults (the server's `contact_defaults`: a Woman/Man
 *  voice group, a speaking style, a speed). An override is saved whole per General and **Reset**
 *  removes it. A voice per language speaks while the call is in that language. What the TTS server
 *  offers is what was discovered from it, never assumed: the style column shows only when it
 *  advertised styles, and a saved voice it no longer lists is marked **Missing** with what speaks
 *  instead. Each row's controls name their General, so a screen reader never hears thirteen
 *  identical "Preview" buttons. */
import { ChevronDown, ChevronUp, Play, Plus, RotateCcw, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { CONTACTS, type HqContact } from "../components/hq/contacts";
import b from "../components/ui/actionButton.module.css";
import { api, ApiError } from "../lib/api";
import type { ContactDefaults, ContactVoice, VoiceSettings, VoiceSettingsPatch } from "../types/voice";
import { languageName, playWav } from "./hqCallsAudio";
import h from "./HqCallsSettings.module.css";
import styles from "./Settings.module.css";

const SPEEDS = [0.75, 0.85, 0.9, 0.95, 1, 1.05, 1.1, 1.2, 1.35, 1.5];
const LANGUAGE_VOICES_MAX = 12;

const speedWord = (n: number) => `${Number(n.toFixed(2))}×`;
const shortName = (c: HqContact) => `General ${c.name.split(" ").at(-1)}`;

/** A profile with only the fields that are set, or null when nothing is (= the defaults). */
/** One queued change to a General's profile, applied to the profile it lands on. */
type Edit = (p: ContactVoice) => ContactVoice;

function clean(p: ContactVoice): ContactVoice | null {
  const out: ContactVoice = {};
  if (p.voice?.trim()) out.voice = p.voice.trim();
  if (p.style) out.style = p.style;
  if (typeof p.speed === "number") out.speed = p.speed;
  const lv = Object.fromEntries(
    Object.entries(p.language_voices ?? {}).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()]),
  );
  if (Object.keys(lv).length) out.language_voices = lv;
  return Object.keys(out).length ? out : null;
}

interface RowProps {
  c: HqContact;
  def: ContactDefaults;
  saved: ContactVoice | undefined;
  groupVoice: string;
  voices: string[];
  styleList: string[];
  languages: string[];
  /** Languages the server can preview in (its fixed sentences). */
  previewable: ReadonlySet<string>;
  onSave: (id: string, profile: ContactVoice | null) => Promise<boolean>;
}

function ContactRow({
  c,
  def,
  saved,
  groupVoice,
  voices,
  styleList,
  languages,
  previewable,
  onSave,
}: RowProps) {
  const uid = useId();
  const [voice, setVoice] = useState(saved?.voice ?? "");
  const [open, setOpen] = useState(false);
  const [langDraft, setLangDraft] = useState<Record<string, string>>(saved?.language_voices ?? {});
  const [addLang, setAddLang] = useState("");
  const [addVoice, setAddVoice] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The last profile the SERVER confirmed for this row (undefined: follow the `saved` props), and
  // the edits queued on top of it.
  const confirmed = useRef<ContactVoice | null | undefined>(undefined);
  const queued = useRef<Edit[]>([]);
  const chain = useRef<Promise<void>>(Promise.resolve());
  // A new saved profile (this row's save, a Reset) resets the drafts — during render, so the row
  // keeps its identity and focus stays where it is. A field edited since the previous saved
  // profile keeps the edit: typing a voice while an earlier edit of this row is saving must not
  // lose what is typed (#1640 second review).
  const savedKey = JSON.stringify(saved ?? null);
  const [seen, setSeen] = useState(savedKey);
  if (seen !== savedKey) {
    const prev = JSON.parse(seen) as ContactVoice | null;
    setSeen(savedKey);
    if (voice === (prev?.voice ?? "")) setVoice(saved?.voice ?? "");
    const prevLv = prev?.language_voices ?? {};
    const next: Record<string, string> = { ...(saved?.language_voices ?? {}) };
    for (const [lang, v] of Object.entries(langDraft)) if (v !== prevLv[lang]) next[lang] = v;
    for (const lang of Object.keys(prevLv)) if (!(lang in langDraft)) delete next[lang]; // removed here
    setLangDraft(next);
  }
  useEffect(() => {
    if (!queued.current.length) confirmed.current = undefined; // the props caught up: follow them
  }, [savedKey]);
  const custom = !!saved;
  const known = (v: string) => !voices.length || voices.includes(v);
  const ownVoice = saved?.voice && known(saved.voice) ? saved.voice : groupVoice;
  const lvSaved = saved?.language_voices ?? {};
  const groupWord = def.group === "man" ? "Man" : "Woman";

  // A profile is saved WHOLE (#1640 reviews). Each control queues an EDIT — "this voice", "drop
  // the French voice", "speed 1.1" — not a whole profile built at click time. Edits are sent one
  // after another, each applied to the profile the server last CONFIRMED: two quick edits keep
  // each other, and a refused edit simply drops out — a later one never carries it along.
  const base = (): ContactVoice =>
    (confirmed.current === undefined ? saved : confirmed.current) ?? {};
  /** What the row will hold once every queued edit lands (the controls compare against it). */
  const current = (): ContactVoice => queued.current.reduce((p, e) => e(p), base());
  const save = (edit: Edit) => {
    queued.current.push(edit);
    setErr(null);
    chain.current = chain.current.then(async () => {
      const next = clean(edit(base())); // null: nothing left (Reset)
      const ok = await onSave(c.id, next);
      queued.current.shift();
      if (ok) confirmed.current = next;
      else setErr("Couldn’t save — please try again.");
    });
    return chain.current;
  };
  const commitVoice = () => {
    const v = voice.trim();
    if ((current().voice ?? "") !== v) void save((p) => ({ ...p, voice: v || undefined }));
  };
  const commitLang = (lang: string) => {
    const v = (langDraft[lang] ?? "").trim();
    if (!v || v === (current().language_voices ?? {})[lang]) return;
    void save((p) => ({ ...p, language_voices: { ...(p.language_voices ?? {}), [lang]: v } }));
  };
  const removeLang = (lang: string) => {
    setLangDraft((d) => {
      const n = { ...d };
      delete n[lang];
      return n;
    });
    void save((p) => {
      const rest = { ...(p.language_voices ?? {}) };
      delete rest[lang];
      return { ...p, language_voices: rest };
    });
  };
  const add = () => {
    const lang = addLang;
    const v = addVoice.trim();
    if (!lang || !v) return;
    setLangDraft((d) => ({ ...d, [lang]: v }));
    void save((p) => ({ ...p, language_voices: { ...(p.language_voices ?? {}), [lang]: v } }));
    setAddLang("");
    setAddVoice("");
  };
  const preview = async (key: string, v?: string, language?: string) => {
    setBusy(key);
    setErr(null);
    try {
      await playWav(await api.voicePreviewContact(c.id, v, language));
    } catch (e) {
      setErr(e instanceof ApiError && e.message ? e.message : "The preview could not play.");
    } finally {
      setBusy(null);
    }
  };

  const langs = Object.keys(lvSaved);
  const addable = languages.filter((l) => !(l in lvSaved));
  const listId = `${uid}-voices`;
  const voiceMissing = !!saved?.voice && !known(saved.voice);
  // Preview what a call would speak: a saved voice gone from the server falls back to the group
  // voice (the server resolves it), unless another voice is typed in its place (#1640 review 6912).
  const typed = voice.trim();
  const previewVoice = typed && !(voiceMissing && typed === saved?.voice) ? typed : undefined;
  return (
    <li className={`${h.prow} ${styleList.length ? "" : h.noStyle}`} data-testid="hq-calls-contact" data-contact={c.id} data-custom={custom}>
      <img className={h.ppic} src={c.poster} alt="" />
      <div className={h.pname}>
        <b>{c.name}</b>
        <span data-testid="hq-calls-contact-kind">{custom ? "Custom" : "Default"}</span>
      </div>
      <datalist id={listId}>
        {voices.map((v) => (
          <option key={v} value={v} />
        ))}
      </datalist>
      <div className={`${h.pfield} ${h.pvoice}`}>
        <input
          className={styles.aiInput}
          aria-label={`Voice for ${c.name}`}
          list={listId}
          spellCheck={false}
          placeholder={groupVoice || `${groupWord} voice`}
          value={voice}
          onChange={(e) => setVoice(e.target.value)}
          onBlur={commitVoice}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitVoice();
          }}
          data-testid="hq-calls-contact-voice"
        />
        {voiceMissing ? (
          <span className="nj-stamp is-warn" data-testid="hq-calls-contact-missing">
            Missing
          </span>
        ) : null}
      </div>
      {styleList.length ? (
        <select
          className={`${styles.aiInput} ${h.pfield} ${h.pstyle}`}
          aria-label={`Style for ${c.name}`}
          value={saved?.style ?? ""}
          onChange={(e) => {
            const style = e.target.value || undefined;
            void save((p) => ({ ...p, style }));
          }}
          data-testid="hq-calls-contact-style"
        >
          <option value="">Default · {def.style}</option>
          {styleList.map((st) => (
            <option key={st} value={st}>
              {st}
            </option>
          ))}
        </select>
      ) : null}
      <select
        className={`${styles.aiInput} ${h.pfield} ${h.pspeed}`}
        aria-label={`Speed for ${c.name}`}
        value={saved?.speed === undefined ? "" : String(saved.speed)}
        onChange={(e) => {
          const speed = e.target.value === "" ? undefined : Number(e.target.value);
          void save((p) => ({ ...p, speed }));
        }}
        data-testid="hq-calls-contact-speed"
      >
        <option value="">Default · {speedWord(def.speed)}</option>
        {SPEEDS.map((n) => (
          <option key={n} value={String(n)}>
            {speedWord(n)}
          </option>
        ))}
      </select>
      <div className={h.pacts}>
        <button
          type="button"
          className={b.icon}
          aria-label={`Preview ${c.name}`}
          title="Preview"
          disabled={busy !== null}
          onClick={() => void preview("own", previewVoice)}
          data-testid="hq-calls-contact-preview"
        >
          <Play size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={b.ghost}
          aria-expanded={open}
          aria-controls={`${uid}-lv`}
          aria-label={`Voices per language for ${c.name}`}
          onClick={() => setOpen(!open)}
          data-testid="hq-calls-contact-languages"
        >
          Languages{langs.length ? ` · ${langs.length}` : ""}
          {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
        </button>
        {custom ? (
          <button
            type="button"
            className={b.icon}
            aria-label={`Reset ${c.name} to default`}
            title="Reset to default"
            onClick={() => void save(() => ({}))}
            data-testid="hq-calls-contact-reset"
          >
            <RotateCcw size={16} aria-hidden="true" />
          </button>
        ) : null}
      </div>
      {voiceMissing ? (
        <p className={h.pnote} data-testid="hq-calls-contact-missing-note">
          “{saved!.voice}” is no longer on your server — {shortName(c)} falls back to the {groupWord} voice
          {groupVoice ? ` (${groupVoice})` : ""} until you pick one.
        </p>
      ) : null}
      {err ? (
        <p className={`${styles.err} ${h.pnote}`} role="alert">
          {err}
        </p>
      ) : null}
      {open ? (
        <div className={h.lv} id={`${uid}-lv`} data-testid="hq-calls-contact-lv">
          <span className={h.lvh}>Voice per language — speaks while the call is in that language</span>
          {langs.map((lang) => {
            const name = languageName(lang);
            const missing = !known(lvSaved[lang]);
            return (
              <div key={lang} className={h.lvrow} data-testid="hq-calls-lv-row" data-lang={lang}>
                <span className={h.lang}>{lang.toUpperCase()}</span>
                <input
                  className={styles.aiInput}
                  aria-label={`${name} voice for ${c.name}`}
                  list={listId}
                  spellCheck={false}
                  value={langDraft[lang] ?? lvSaved[lang]}
                  onChange={(e) => setLangDraft((d) => ({ ...d, [lang]: e.target.value }))}
                  onBlur={() => commitLang(lang)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitLang(lang);
                  }}
                />
                <div className={h.pacts}>
                  {missing || !previewable.has(lang) ? null : (
                    <button
                      type="button"
                      className={b.icon}
                      aria-label={`Preview ${c.name} in ${name}`}
                      title={`Preview in ${name}`}
                      disabled={busy !== null}
                      onClick={() => void preview(lang, lvSaved[lang], lang)}
                    >
                      <Play size={16} aria-hidden="true" />
                    </button>
                  )}
                  <button
                    type="button"
                    className={b.icon}
                    aria-label={`Remove the ${name} voice for ${c.name}`}
                    title="Remove"
                    onClick={() => removeLang(lang)}
                  >
                    <X size={16} aria-hidden="true" />
                  </button>
                </div>
                {missing ? (
                  <p className={h.lvnote}>
                    <span className="nj-stamp is-warn" data-testid="hq-calls-lv-missing">
                      Missing
                    </span>{" "}
                    “{lvSaved[lang]}” is no longer on your server: on {name} calls {shortName(c)} speaks
                    with their own voice ({ownVoice || `the ${groupWord} voice`}) until you pick another.
                  </p>
                ) : !previewable.has(lang) ? (
                  <p className={h.lvnote} data-testid="hq-calls-lv-no-preview">
                    There’s no preview sentence in {name} yet; {name} calls still use this voice.
                  </p>
                ) : null}
              </div>
            );
          })}
          {langs.length < LANGUAGE_VOICES_MAX && addable.length ? (
            <div className={h.lvadd}>
              <select
                className={styles.aiInput}
                aria-label={`Language to add for ${c.name}`}
                value={addLang}
                onChange={(e) => setAddLang(e.target.value)}
                data-testid="hq-calls-lv-add-lang"
              >
                <option value="">Add a language…</option>
                {addable.map((l) => (
                  <option key={l} value={l}>
                    {languageName(l)}
                  </option>
                ))}
              </select>
              <input
                className={styles.aiInput}
                aria-label={`Voice for the new language for ${c.name}`}
                list={listId}
                spellCheck={false}
                placeholder="voice"
                value={addVoice}
                onChange={(e) => setAddVoice(e.target.value)}
                data-testid="hq-calls-lv-add-voice"
              />
              <button
                type="button"
                className={b.ghost}
                disabled={!addLang || !addVoice.trim()}
                onClick={add}
                data-testid="hq-calls-lv-add"
              >
                <Plus size={14} aria-hidden="true" /> Add
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export function HqCallsProfiles({
  settings,
  saveBlock,
  voiceList,
}: {
  settings: VoiceSettings;
  /** A settings write through the page's single queue; the page shows what it stores. */
  saveBlock: (patch: VoiceSettingsPatch) => Promise<VoiceSettings>;
  /** The TTS server's voices: a fresh read, else the last saved one. */
  voiceList: string[];
}) {
  const [langErr, setLangErr] = useState<string | null>(null);
  const previewLanguages = settings.preview_languages ?? ["en"];
  const previewable = new Set(previewLanguages);
  // Offered when the TTS server lists no languages: the ones a Preview can speak.
  const languages = settings.tts_languages.length ? settings.tts_languages : previewLanguages;
  const callLanguages = Array.from(
    new Set(["en", ...languages, ...(settings.language !== "auto" ? [settings.language] : [])]),
  );

  const saveContact = async (id: string, profile: ContactVoice | null) => {
    try {
      await saveBlock({ contacts: { [id]: profile } });
      return true;
    } catch {
      return false;
    }
  };
  const saveLanguage = async (language: string) => {
    setLangErr(null);
    try {
      await saveBlock({ language });
    } catch (e) {
      setLangErr(e instanceof ApiError && e.message ? e.message : "Couldn’t save — please try again.");
    }
  };

  return (
    <>
      <section className={styles.section} aria-labelledby="hq-calls-lang-h">
        <h2 id="hq-calls-lang-h">Call language</h2>
        <div className={styles.aiField}>
          <select
            className={styles.aiInput}
            aria-labelledby="hq-calls-lang-h"
            value={settings.language}
            onChange={(e) => void saveLanguage(e.target.value)}
            data-testid="hq-calls-language"
          >
            <option value="auto">Auto — follows you</option>
            {callLanguages.map((l) => (
              <option key={l} value={l}>
                {languageName(l)}
              </option>
            ))}
          </select>
        </div>
        <p className={styles.hint} data-testid="hq-calls-language-hint">
          {settings.language !== "auto"
            ? `Calls are in ${languageName(settings.language)}, whatever you speak.`
            : settings.stt_detects_language === true
              ? "Auto: HQ answers in the language you speak (your speech server reports it)."
              : settings.stt_detects_language === false
                ? "Auto: this speech server can't tell languages apart, so calls are English. Pick a language to change it."
                : "Auto: calls are English until the speech server is tested."}{" "}
          {settings.stt_detects_language === null
            ? "Saying “confirm” works once Test both has checked the speech server; until then, cards are confirmed on screen."
            : "Saying “confirm” works on English calls only; on other languages, cards are confirmed on screen."}
        </p>
        {langErr ? (
          <p className={styles.err} role="alert">
            {langErr}
          </p>
        ) : null}
      </section>

      <section className={styles.section} aria-labelledby="hq-calls-generals-h">
        <h2 id="hq-calls-generals-h">Each General's voice</h2>
        <p className={styles.hint}>
          Each General starts with a style that fits them. Add a voice for another language and it
          speaks while the call is in that language. Accents come only from voices that have them
          natively — never an imitation.
        </p>
        <ul className={h.plist} data-testid="hq-calls-contacts">
          {CONTACTS.map((c) => {
            const def = settings.contact_defaults?.[c.id] ?? { group: c.id.endsWith("-man") ? "man" : "woman", style: "neutral", speed: 1 };
            return (
              <ContactRow
                key={c.id}
                c={c}
                def={def}
                saved={settings.contacts?.[c.id]}
                groupVoice={settings.voices[def.group] || settings.voices.woman}
                voices={voiceList}
                styleList={settings.tts_styles}
                languages={languages}
                previewable={previewable}
                onSave={saveContact}
              />
            );
          })}
        </ul>
      </section>

      {!settings.tts_styles.length || settings.stt_detects_language === false ? (
        <section className={styles.section} aria-labelledby="hq-calls-unsup-h" data-testid="hq-calls-unsupported">
          <h2 id="hq-calls-unsup-h">When your server doesn't offer it</h2>
          {!settings.tts_styles.length ? (
            <p className={h.unsup}>
              <span className="nj-stamp">Styles</span>
              <span>This server doesn't list speaking styles — the style column is hidden and nothing is sent.</span>
            </p>
          ) : null}
          {settings.stt_detects_language === false ? (
            <p className={h.unsup}>
              <span className="nj-stamp">Languages</span>
              <span>
                This server doesn't report the language it hears. On Auto, calls are in English; pick a
                language above to change it.
              </span>
            </p>
          ) : null}
        </section>
      ) : null}
    </>
  );
}
