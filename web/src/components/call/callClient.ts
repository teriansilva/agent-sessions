/** One call to HQ (#1397 P1), as a state machine the UI subscribes to.
 *
 *  `start()` runs on the Call tap: it acquires the microphone (kept silent), asks the server to
 *  admit a call, opens `/ws/voice/{id}` through `openAppSocket` (so a Home Free page would ride its
 *  tunnel — the server refuses calls there for now, with the reason), and proves itself with the
 *  CSRF token in its first message. Audio flows only after the server says `connected`.
 *
 *  Every way a call ends goes through `finish()`: the microphone's tracks stop, queued speech is
 *  dropped, the socket closes, and the reason is kept for the dropped state. Nothing reconnects
 *  or reopens the microphone on its own; calling again is a new tap and a new `CallClient`.
 *
 *  Captions: the operator's lines arrive as heard; HQ's arrive when HQ starts saying them, muted
 *  until playback reaches them (`speaking` on → that line is being spoken, off → spoken), and an
 *  interrupt leaves the unspoken rest muted ("cut").
 *
 *  Spoken confirmation (#1397 P2): `said` puts a marker behind the queued speech and `played` goes
 *  back only when playback reaches it (an interrupt flushes it: no ack). `challenge` names the cards
 *  a spoken "confirm" may settle and for how long; `settled` is what the card's route answered. A
 *  tap on any card during the call says `card_decided`, which voids the spoken window. */
import type { CallServerMessage, CallState, CardSettlement } from "../../types/voice";
import type { CallAudio } from "./callAudio";

export type CallPhase = "idle" | "starting" | "dialing" | "connected" | "ended";

export interface Caption {
  id: string;
  who: "you" | "hq";
  text: string;
  kind?: string;
  turnId?: string | null;
  /** HQ's lines only: where playback is. */
  status?: "queued" | "speaking" | "spoken" | "cut";
}

export interface CallEnd {
  reason: string;
  detail: string;
  endpoint: string;
}

export interface CallSnapshot {
  phase: CallPhase;
  serverState: CallState;
  callId: string | null;
  conversationId: string | null;
  contact: string | null;
  captions: Caption[];
  muted: boolean;
  userSpeaking: boolean;
  hqSpeaking: boolean;
  startedAt: number | null;
  connectedAt: number | null;
  ended: CallEnd | null;
  /** Bumped by every `turn` message: the cards on screen are re-read. */
  turnSeq: number;
  lastTurnId: string | null;
  progress: string | null;
  /** The cards a spoken "confirm" may settle, in their spoken order, and until when (ms). */
  challenge: { cards: string[]; expiresAt: number } | null;
  /** Cards settled by voice on this call. */
  settled: Record<string, CardSettlement>;
  /** Bumped by every `settled`: the cards on screen are re-read. */
  cardSeq: number;
}

export interface CallDeps {
  audio: () => CallAudio;
  admit: (body: { contact?: string; conversation_id?: string | null }) => Promise<{
    id: string;
    conversation_id: string;
  }>;
  hangUp: (id: string) => Promise<unknown>;
  openSocket: (url: string) => WebSocket;
  csrf: () => string;
  now?: () => number;
}

export const CAPTIONS_KEPT = 40;

export const IDLE: CallSnapshot = {
  phase: "idle",
  serverState: "dialing",
  callId: null,
  conversationId: null,
  contact: null,
  captions: [],
  muted: false,
  userSpeaking: false,
  hqSpeaking: false,
  startedAt: null,
  connectedAt: null,
  ended: null,
  turnSeq: 0,
  lastTurnId: null,
  progress: null,
  challenge: null,
  settled: {},
  cardSeq: 0,
};

function socketUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

/** The error text an admission refusal carries (an `ApiError` message is the server's detail). */
function detailOf(e: unknown): string {
  return e instanceof Error && e.message ? e.message : "the call could not start";
}

export class CallClient {
  private snap: CallSnapshot = IDLE;
  private listeners = new Set<() => void>();
  private audio: CallAudio | null = null;
  private ws: WebSocket | null = null;
  private readonly now: () => number;
  private readonly deps: CallDeps;

  constructor(deps: CallDeps) {
    this.deps = deps;
    this.now = deps.now ?? (() => Date.now());
  }

  get snapshot(): CallSnapshot {
    return this.snap;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  private set(patch: Partial<CallSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    for (const fn of this.listeners) fn();
  }

  get active(): boolean {
    return this.snap.phase === "starting" || this.snap.phase === "dialing" || this.snap.phase === "connected";
  }

  async start(opts: { contact: string | null; conversationId?: string | null }): Promise<void> {
    if (this.active) return;
    this.set({ ...IDLE, phase: "starting", contact: opts.contact, startedAt: this.now() });
    const audio = this.deps.audio();
    this.audio = audio;
    try {
      await audio.prepare();
    } catch (e) {
      if (this.hasEnded()) return; // hung up while the prompt was up: keep that reason
      const denied = e instanceof DOMException && /NotAllowed|Security/.test(e.name);
      this.finish(denied ? "mic_denied" : "mic_failed", denied ? "" : detailOf(e));
      return;
    }
    if (this.snap.phase !== "starting") return; // ended while the prompt was up
    let call: { id: string; conversation_id: string };
    try {
      call = await this.deps.admit({
        ...(opts.contact ? { contact: opts.contact } : {}),
        conversation_id: opts.conversationId ?? null,
      });
    } catch (e) {
      if (this.hasEnded()) return; // hung up meanwhile: keep that reason
      this.finish("refused", detailOf(e));
      return;
    }
    if (this.snap.phase !== "starting") {
      void this.deps.hangUp(call.id).catch(() => undefined);
      return;
    }
    this.set({ phase: "dialing", callId: call.id, conversationId: call.conversation_id });
    let ws: WebSocket;
    try {
      ws = this.deps.openSocket(socketUrl(`/ws/voice/${encodeURIComponent(call.id)}`));
    } catch (e) {
      // The call is admitted: end it by id, or it holds the server's one call slot until its
      // dial timer runs out.
      void this.deps.hangUp(call.id).catch(() => undefined);
      if (this.hasEnded()) return;
      this.finish("disconnected", detailOf(e));
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ t: "hello", csrf: this.deps.csrf() }));
    ws.onmessage = (e: MessageEvent) => this.onMessage(e.data);
    ws.onclose = (e: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.active) {
        // Closed before the call ended (refused, dropped, never joined): end it by id too, so the
        // server's one call slot frees now (a no-op for a call the server already ended).
        if (this.snap.callId) void this.deps.hangUp(this.snap.callId).catch(() => undefined);
        this.finish(
          e.code === 4409 ? "home_free" : e.code === 4403 || e.code === 4401 ? "refused" : "disconnected",
          e.reason || "",
        );
      }
    };
  }

  private onMessage(data: unknown): void {
    if (!this.active) return;
    if (data instanceof ArrayBuffer) {
      if (this.snap.phase === "connected") this.audio?.play(data);
      return;
    }
    if (typeof data !== "string") return;
    let msg: CallServerMessage;
    try {
      msg = JSON.parse(data) as CallServerMessage;
    } catch {
      return;
    }
    this.handle(msg);
  }

  /** One server message. Public for the unit tests. */
  handle(msg: CallServerMessage): void {
    switch (msg.t) {
      case "state":
        if (msg.state === "ended") return;
        this.set({ serverState: msg.state, progress: msg.state === "thinking" ? this.snap.progress : null });
        return;
      case "connected":
        if (this.snap.phase !== "dialing") return;
        this.set({ phase: "connected", connectedAt: this.now(), serverState: "listening" });
        this.audio?.start((frame) => {
          if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(frame);
        });
        return;
      case "caption": {
        const cap: Caption = {
          id: msg.id,
          who: msg.who,
          text: msg.text,
          kind: msg.kind,
          turnId: msg.turn_id ?? null,
          status: msg.who === "hq" ? "queued" : undefined,
        };
        this.set({ captions: [...this.snap.captions, cap].slice(-CAPTIONS_KEPT) });
        return;
      }
      case "speaking": {
        const caps = this.snap.captions.map((c) => ({ ...c }));
        if (msg.on) {
          const next = caps.find((c) => c.who === "hq" && c.status === "queued");
          if (next) next.status = "speaking";
        } else {
          for (const c of caps) if (c.status === "speaking") c.status = "spoken";
        }
        this.set({ hqSpeaking: msg.on, captions: caps });
        return;
      }
      case "interrupt": {
        this.audio?.flush();
        const caps = this.snap.captions.map((c) =>
          c.who === "hq" && (c.status === "queued" || c.status === "speaking")
            ? { ...c, status: "cut" as const }
            : c,
        );
        this.set({ hqSpeaking: false, captions: caps });
        return;
      }
      case "user":
        this.set({ userSpeaking: msg.speaking });
        return;
      case "muted":
        this.set({ muted: msg.on });
        return;
      case "progress":
        this.set({ progress: msg.step });
        return;
      case "turn":
        this.set({
          turnSeq: this.snap.turnSeq + 1,
          lastTurnId: typeof msg.turn?.turn_id === "string" ? msg.turn.turn_id : this.snap.lastTurnId,
        });
        return;
      case "ended":
        this.finish(msg.reason || "ended", msg.detail || "", msg.endpoint || "");
        return;
      case "said": {
        if (this.snap.phase !== "connected" || typeof msg.id !== "string") return;
        const ws = this.ws;
        this.audio?.mark(msg.id, (id) => {
          // Only on the socket that said it, and only while the call is still up.
          if (this.ws === ws && this.active && ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ t: "played", id }));
          }
        });
        return;
      }
      case "challenge": {
        const cards = Array.isArray(msg.cards) ? msg.cards.filter((c) => typeof c === "string") : [];
        const secs = typeof msg.expires_s === "number" && msg.expires_s > 0 ? msg.expires_s : 20;
        this.set({ challenge: cards.length ? { cards, expiresAt: this.now() + secs * 1000 } : null });
        return;
      }
      case "settled": {
        if (typeof msg.card !== "string") return;
        const prev = this.snap.settled[msg.card];
        const next: CardSettlement =
          "final" in msg
            ? { ok: prev?.ok ?? true, status: prev?.status ?? 202, final: msg.final }
            : { ok: !!msg.ok, status: typeof msg.status === "number" ? msg.status : 0 };
        this.set({
          settled: { ...this.snap.settled, [msg.card]: next },
          cardSeq: this.snap.cardSeq + 1,
        });
        return;
      }
    }
  }

  /** The "What needs me?" chip: the same read-out as asking it aloud (the server ignores it while
   *  an HQ turn is running). */
  askNeedsYou(): void {
    if (this.snap.phase !== "connected" || this.ws?.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ t: "needs_you" }));
  }

  /** A tap decided a card during the call: the spoken window for it closes (server and screen). */
  cardDecided(id: string): void {
    if (!this.active) return;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ t: "card_decided", id }));
    }
    if (this.snap.challenge?.cards.includes(id)) this.set({ challenge: null });
  }

  setMuted(on: boolean): void {
    if (!this.active) return;
    this.audio?.setMuted(on);
    this.set({ muted: on });
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ t: "mute", on }));
  }

  /** Hang up. The socket says so if it is open; otherwise the admitted call is ended by id. */
  end(): void {
    if (!this.active) return;
    const id = this.snap.callId;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ t: "end" }));
    } else if (id) {
      void this.deps.hangUp(id).catch(() => undefined);
    }
    this.finish("hung_up");
  }

  /** Read fresh (not narrowed): an `end()` may have run during an await. */
  private hasEnded(): boolean {
    return this.snap.phase === "ended";
  }

  private finish(reason: string, detail = "", endpoint = ""): void {
    this.audio?.stop();
    this.audio = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      try {
        ws.close(1000);
      } catch {
        /* already closed */
      }
    }
    const caps = this.snap.captions.map((c) =>
      c.who === "hq" && (c.status === "queued" || c.status === "speaking") ? { ...c, status: "cut" as const } : c,
    );
    this.set({
      phase: "ended",
      serverState: "ended",
      hqSpeaking: false,
      userSpeaking: false,
      captions: caps,
      challenge: null,
      ended: { reason, detail, endpoint },
    });
  }

  /** Back to idle once the operator has read the ended state. */
  reset(): void {
    if (this.active) return;
    this.set(IDLE);
  }

  inputLevel(): number {
    return this.audio?.inputLevel() ?? 0;
  }

  outputLevel(): number {
    return this.audio?.outputLevel() ?? 0;
  }
}

/** Ends the operator chose (#1668): the hang-up, HQ ending the call on their goodbye, or no
 *  answer to HQ's still-there question. Everything else is a dropped call. */
export function normalEnd(reason: string): boolean {
  return reason === "hung_up" || reason === "hq_ended" || reason === "no_answer";
}

/** What the dropped state says, from the server's reason and the configured hosts. */
export function endedText(end: CallEnd, hosts: { stt: string; tts: string }): string {
  switch (end.reason) {
    case "hung_up":
      return "Call ended.";
    case "hq_ended":
      return "HQ ended the call.";
    case "no_answer":
      return "No answer — the call ended.";
    case "tts_failed":
      return `${hosts.tts || "The text-to-speech endpoint"} (text-to-speech) stopped answering.`;
    case "stt_failed":
      return `${hosts.stt || "The speech-to-text endpoint"} (speech-to-text) stopped answering.`;
    case "worker_failed":
      return "The voice worker stopped.";
    case "idle":
      return "The call ended after five quiet minutes.";
    case "max_duration":
      return "The call reached its 60-minute limit.";
    case "signed_out":
      return "You were signed out, so the call ended.";
    case "server_stopping":
      return "BattleLab restarted, so the call ended.";
    case "mic_denied":
      return "The microphone is blocked for this page. Allow it in the browser to call HQ.";
    case "mic_failed":
      return "The microphone could not start.";
    case "home_free":
      return "Calls over Home Free are not available yet. Call from your local network.";
    case "refused":
      return end.detail ? `The call was refused: ${end.detail}` : "The call was refused.";
    default:
      return "The connection dropped.";
  }
}

/** What the live region says: the phase, nothing more. */
export function phaseAnnouncement(snap: CallSnapshot, name: string): string {
  switch (snap.phase) {
    case "starting":
    case "dialing":
      return `Calling HQ${name ? `, ${name}` : ""}.`;
    case "connected":
      return `Connected to HQ${name ? `, ${name}` : ""}.`;
    case "ended":
      return "Call ended.";
    default:
      return "";
  }
}
