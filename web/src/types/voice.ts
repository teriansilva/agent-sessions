/** Calling HQ (#1397 P1): the shapes of `/api/voice/*` and the call socket's messages.
 *  Server side: `routes/voice.py`, `voice_calls.py`, `hq_calls_settings.py`. */

export type TurnTaking = "quick" | "natural" | "patient";
export type MicMode = "open" | "push";
export type AnswerLength = "brief" | "detailed";
export type VoiceGroup = "woman" | "man";
/** Where an endpoint sits, derived from its URL by the server. */
export type HostKind = "loopback" | "lan" | "remote" | "invalid";

/** One speech side as the server returns it: never the token, only whether one is stored. */
export interface SpeechSidePublic {
  base_url: string;
  model: string;
  api_key_set: boolean;
}

export interface VoiceSettings {
  stt: SpeechSidePublic;
  tts: SpeechSidePublic;
  voices: Record<VoiceGroup, string>;
  turn_taking: TurnTaking;
  interrupt: boolean;
  mic: MicMode;
  answers: AnswerLength;
  captions: boolean;
  configured: boolean;
  /** #1397 P3: each General's saved overrides (absent = their defaults). */
  contacts: Record<string, ContactVoice>;
  /** Each General's defaults, from the server's roster. */
  contact_defaults: Record<string, ContactDefaults>;
  /** `auto` or a two-letter code. */
  language: string;
  /** What the TTS server advertised when its lists were last read (empty = not advertised). */
  tts_styles: string[];
  tts_languages: string[];
  tts_voices: string[];
  /** The languages the server has a fixed preview sentence in: offered when the TTS server lists
   *  none, and a language outside them shows no Preview (it would only be refused). */
  preview_languages: string[];
  /** Proven by **Test both**: the STT reports the language it heard; the TTS takes `language`.
   *  Detection is `null` until tested (or after the STT endpoint changed): spoken "confirm" is
   *  then refused, never assumed English. */
  stt_detects_language: boolean | null;
  tts_accepts_language: boolean;
}

/** A General's saved voice profile. Saving one replaces it whole; `null` resets it. */
export interface ContactVoice {
  voice?: string;
  style?: string;
  /** 0.75–1.5. */
  speed?: number;
  /** Two-letter language code → voice, up to 12. */
  language_voices?: Record<string, string>;
}

export interface ContactDefaults {
  group: VoiceGroup;
  style: string;
  speed: number;
}

/** A patch for one side. `api_key`: a string replaces the token, `null` removes it, absent (or
 *  blank) keeps it. */
export interface SpeechSidePatch {
  base_url?: string;
  model?: string;
  api_key?: string | null;
}

export interface VoiceSettingsPatch {
  stt?: SpeechSidePatch;
  tts?: SpeechSidePatch;
  voices?: Partial<Record<VoiceGroup, string>>;
  turn_taking?: TurnTaking;
  interrupt?: boolean;
  mic?: MicMode;
  answers?: AnswerLength;
  captions?: boolean;
  contacts?: Record<string, ContactVoice | null>;
  language?: string;
  tts_styles?: string[];
  tts_languages?: string[];
  tts_voices?: string[];
  stt_detects_language?: boolean | null;
  tts_accepts_language?: boolean;
}

export interface ConformanceCheck {
  id: string;
  ok: boolean;
  required: boolean;
  note: string;
  value: unknown;
}

export interface ConformanceReport {
  usable: boolean;
  checks: ConformanceCheck[];
}

export interface VoiceLists {
  models: string[] | null;
  voices: string[] | null;
  /** TTS only (#1397 P3): what the server advertises, `null` when it publishes none. */
  styles?: string[] | null;
  languages?: string[] | null;
}

/** Speech on this machine (#1397 P4): `GET /api/voice/local`. Never the token, only whether one
 *  exists. */
export interface VoiceLocal {
  installed: { runtime: boolean; server: boolean };
  token: boolean;
  /** The user unit's state: true running, false not, null no unit. */
  unit: boolean | null;
  /** A user service manager is available (`systemctl --user`). */
  systemd: boolean;
  stt: string | null;
  last_error: string | null;
  stale: string | null;
  url: string;
  running: boolean;
  in_use: boolean;
  voices: Record<VoiceGroup, string>;
  /** `hard`: the user service's memory/CPU caps apply; `soft`: `voice serve`, in-process limits only. */
  limits?: "hard" | "soft";
}

/** `POST /api/voice/local/use` → 200. */
export interface VoiceLocalUse {
  settings: VoiceSettings;
  report: ConformanceReport;
}

export interface VoiceDestinations {
  stt: HostKind | null;
  tts: HostKind | null;
  ai: HostKind | null;
  stt_host: string;
  tts_host: string;
  ai_host: string;
}

export interface VoiceCall {
  id: string;
  conversation_id: string;
  contact: string;
  state: CallState;
  muted: boolean;
  elapsed_s: number;
  ended_reason: string;
}

export interface VoiceCapabilities {
  configured: boolean;
  runtime: boolean;
  available: boolean;
  reason: string;
  /** #1397 P4: the runtime is overridden, or its setup is stale. Empty when neither. */
  runtime_note?: string;
  destinations: VoiceDestinations;
  captions: boolean;
  mic: MicMode;
  active_call: VoiceCall | null;
}

/** The server's view of the call: what HQ is doing right now. */
export type CallState = "dialing" | "listening" | "thinking" | "speaking" | "ended";

/** Text messages the call socket sends. Binary messages are speech (PCM16 mono, 24 kHz). */
export type CallServerMessage =
  | { t: "state"; state: CallState }
  | { t: "connected" }
  | {
      t: "caption";
      who: "you" | "hq";
      id: string;
      text: string;
      kind?: "greeting" | "answer" | "ack" | "progress" | "error" | "readback" | "outcome";
      turn_id?: string | null;
    }
  | { t: "turn"; turn: { turn_id?: string; status?: string; [k: string]: unknown } }
  | { t: "progress"; step: string }
  | { t: "interrupt" }
  | { t: "speaking"; id?: string; on: boolean }
  | { t: "user"; speaking: boolean }
  | { t: "muted"; on: boolean }
  | { t: "ended"; reason: string; detail?: string; endpoint?: string }
  /** #1397 P2: this say's audio has been SENT (it is queued behind it); ack `played` once it plays. */
  | { t: "said"; id: string }
  /** The cards a spoken "confirm" may settle now, in their spoken order; `[]` clears it. */
  | { t: "challenge"; cards: string[]; expires_s?: number }
  /** A voice confirmation ran the card's route: its status, then (202 only) the end state. */
  | { t: "settled"; card: string; ok: boolean; status: number }
  | { t: "settled"; card: string; final: string | null };

/** What a spoken confirmation did to one card (#1397 P2). `final` arrives only after a 202. */
export interface CardSettlement {
  ok: boolean;
  status: number;
  final?: string | null;
}
