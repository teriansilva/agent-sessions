/** The call state machine (#1397 P1): the microphone stays silent until the server says
 *  `connected`, every way a call ends stops the audio and closes the socket, interrupts flush the
 *  queued speech, and nothing reconnects on its own. */
import { beforeEach, describe, expect, test, vi } from "vitest";

import type { CallAudio } from "./callAudio";
import { CallClient, endedText, phaseAnnouncement, type CallDeps } from "./callClient";

class FakeAudio implements CallAudio {
  prepared = false;
  started = false;
  stopped = false;
  muted = false;
  played: ArrayBuffer[] = [];
  marks: { id: string; onPlayed: (id: string) => void }[] = [];
  flushes = 0;
  send: ((f: ArrayBuffer) => void) | null = null;
  fail: Error | null = null;
  async prepare() {
    if (this.fail) throw this.fail;
    this.prepared = true;
  }
  start(send: (f: ArrayBuffer) => void) {
    this.started = true;
    this.send = send;
  }
  setMuted(on: boolean) {
    this.muted = on;
  }
  play(pcm: ArrayBuffer) {
    this.played.push(pcm);
  }
  mark(id: string, onPlayed: (id: string) => void) {
    this.marks.push({ id, onPlayed });
  }
  flush() {
    this.flushes++;
  }
  stop() {
    this.stopped = true;
  }
  inputLevel() {
    return 0;
  }
  outputLevel() {
    return 0;
  }
}

class FakeSocket {
  static OPEN = 1;
  readyState = 0;
  url: string;
  binaryType = "blob";
  sent: (string | ArrayBuffer)[] = [];
  closed: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  send(d: string | ArrayBuffer) {
    this.sent.push(d);
  }
  close(code: number) {
    this.closed = code;
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  msg(m: object | ArrayBuffer) {
    this.onmessage?.({ data: m instanceof ArrayBuffer ? m : JSON.stringify(m) });
  }
  drop(code = 1006, reason = "") {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }
  json() {
    return this.sent.filter((s): s is string => typeof s === "string").map((s) => JSON.parse(s));
  }
}

let audio: FakeAudio;
let sock: FakeSocket;
let deps: CallDeps;

beforeEach(() => {
  vi.stubGlobal("WebSocket", { OPEN: 1 });
  audio = new FakeAudio();
  deps = {
    audio: () => audio,
    admit: vi.fn(async () => ({ id: "call1", conversation_id: "c".repeat(32) })),
    hangUp: vi.fn(async () => ({})),
    openSocket: vi.fn((url: string) => {
      sock = new FakeSocket(url);
      return sock as unknown as WebSocket;
    }),
    csrf: () => "tok",
    now: () => 1_000,
  };
});

async function dial(contact: string | null = "asia-woman") {
  const c = new CallClient(deps);
  await c.start({ contact });
  return c;
}

describe("starting", () => {
  test("acquires the mic first, admits, opens the socket and proves itself with the CSRF hello", async () => {
    const c = await dial();
    expect(audio.prepared).toBe(true);
    expect(deps.admit).toHaveBeenCalledWith({ contact: "asia-woman", conversation_id: null });
    expect(sock.url).toMatch(/\/ws\/voice\/call1$/);
    expect(sock.binaryType).toBe("arraybuffer");
    sock.open();
    expect(sock.json()).toEqual([{ t: "hello", csrf: "tok" }]);
    expect(c.snapshot.phase).toBe("dialing");
  });

  test("no audio is sent before the server says connected", async () => {
    const c = await dial();
    sock.open();
    expect(audio.started).toBe(false);
    sock.msg(new ArrayBuffer(4));
    expect(audio.played).toHaveLength(0);
    sock.msg({ t: "connected" });
    expect(audio.started).toBe(true);
    expect(c.snapshot.phase).toBe("connected");
    audio.send?.(new ArrayBuffer(640));
    expect(sock.sent.at(-1)).toBeInstanceOf(ArrayBuffer);
  });

  test("hanging up while the microphone prompt is open stays a hang-up (web review of #1640)", async () => {
    let grant!: () => void;
    audio.prepare = () => new Promise<void>((resolve) => (grant = resolve)).then(() => {
      throw new Error("this call's audio has ended"); // what the real audio does once stopped
    });
    const c = new CallClient(deps);
    const starting = c.start({ contact: "asia-woman" });
    c.end(); // Cancel while the browser asks for the microphone
    grant();
    await starting;
    expect(c.snapshot.phase).toBe("ended");
    expect(c.snapshot.ended?.reason).toBe("hung_up"); // never "the microphone could not start"
    expect(audio.stopped).toBe(true);
  });

  test("hanging up while the call is being admitted stays a hang-up", async () => {
    let fail!: (e: Error) => void;
    deps.admit = vi.fn(() => new Promise<never>((_, reject) => (fail = reject)));
    const c = new CallClient(deps);
    const starting = c.start({ contact: "asia-woman" });
    await vi.waitFor(() => expect(deps.admit).toHaveBeenCalled());
    c.end();
    fail(new Error("aborted"));
    await starting;
    expect(c.snapshot.ended?.reason).toBe("hung_up");
  });

  test("a socket that can't even open still ends the admitted call by id (#1640 review)", async () => {
    deps.openSocket = vi.fn(() => {
      throw new Error("blocked");
    });
    const c = await dial();
    expect(c.snapshot.ended?.reason).toBe("disconnected");
    expect(deps.hangUp).toHaveBeenCalledWith("call1"); // the server's one call slot frees now
  });

  test("a socket closed before the call connected ends the admitted call by id", async () => {
    const c = await dial();
    sock.drop(4403, "refused");
    expect(c.snapshot.phase).toBe("ended");
    expect(deps.hangUp).toHaveBeenCalledWith("call1");
  });

  test("a blocked microphone ends the call before anything is admitted", async () => {
    audio.fail = new DOMException("no", "NotAllowedError");
    const c = await dial();
    expect(deps.admit).not.toHaveBeenCalled();
    expect(c.snapshot.phase).toBe("ended");
    expect(c.snapshot.ended?.reason).toBe("mic_denied");
  });

  test("a refused admission keeps the server's reason and stops the mic", async () => {
    deps.admit = vi.fn(async () => {
      throw new Error("calls over Home Free are not available yet");
    });
    const c = await dial();
    expect(c.snapshot.ended).toEqual({
      reason: "refused",
      detail: "calls over Home Free are not available yet",
      endpoint: "",
    });
    expect(audio.stopped).toBe(true);
  });

  test("no contact (HQ Off) admits without one", async () => {
    await dial(null);
    expect(deps.admit).toHaveBeenCalledWith({ conversation_id: null });
  });
});

describe("during the call", () => {
  async function connected() {
    const c = await dial();
    sock.open();
    sock.msg({ t: "connected" });
    return c;
  }

  test("HQ's captions are queued, then spoken; an interrupt flushes audio and cuts the rest", async () => {
    const c = await connected();
    sock.msg({ t: "caption", who: "you", id: "u1", text: "How is api rebase?" });
    sock.msg({ t: "caption", who: "hq", id: "h1", text: "It finished.", kind: "answer" });
    sock.msg({ t: "caption", who: "hq", id: "h2", text: "Two tests fail.", kind: "answer" });
    expect(c.snapshot.captions.map((x) => x.status)).toEqual([undefined, "queued", "queued"]);
    sock.msg({ t: "speaking", on: true });
    expect(c.snapshot.captions[1].status).toBe("speaking");
    sock.msg({ t: "speaking", on: false });
    expect(c.snapshot.captions[1].status).toBe("spoken");
    sock.msg({ t: "speaking", on: true });
    sock.msg({ t: "interrupt" });
    expect(audio.flushes).toBe(1);
    expect(c.snapshot.captions[2].status).toBe("cut");
    expect(c.snapshot.hqSpeaking).toBe(false);
  });

  test("speech audio plays only while connected", async () => {
    await connected();
    sock.msg(new ArrayBuffer(8));
    expect(audio.played).toHaveLength(1);
  });

  test("mute tells the audio and the server", async () => {
    const c = await connected();
    c.setMuted(true);
    expect(audio.muted).toBe(true);
    expect(sock.json().at(-1)).toEqual({ t: "mute", on: true });
    expect(c.snapshot.muted).toBe(true);
  });

  test("turn messages bump the card read", async () => {
    const c = await connected();
    sock.msg({ t: "turn", turn: { turn_id: "t1", status: "done" } });
    expect(c.snapshot.turnSeq).toBe(1);
    expect(c.snapshot.lastTurnId).toBe("t1");
  });

  test("captions are bounded", async () => {
    const c = await connected();
    for (let i = 0; i < 100; i++) sock.msg({ t: "caption", who: "you", id: `u${i}`, text: "x" });
    expect(c.snapshot.captions.length).toBe(40);
  });
});

describe("ending", () => {
  test("hanging up says so on the socket, stops the audio and closes", async () => {
    const c = await dial();
    sock.open();
    sock.msg({ t: "connected" });
    c.end();
    expect(sock.json().at(-1)).toEqual({ t: "end" });
    expect(audio.stopped).toBe(true);
    expect(sock.closed).toBe(1000);
    expect(c.snapshot.ended?.reason).toBe("hung_up");
  });

  test("hanging up before the socket opened ends the admitted call by id", async () => {
    const c = await dial();
    c.end();
    expect(deps.hangUp).toHaveBeenCalledWith("call1");
  });

  test("the server's ended names the endpoint that failed", async () => {
    const c = await dial();
    sock.open();
    sock.msg({ t: "connected" });
    sock.msg({ t: "ended", reason: "tts_failed", detail: "gone", endpoint: "tts" });
    expect(c.snapshot.ended).toEqual({ reason: "tts_failed", detail: "gone", endpoint: "tts" });
    expect(audio.stopped).toBe(true);
    expect(endedText(c.snapshot.ended!, { stt: "a:1", tts: "gpu-1.lan:8880" })).toBe(
      "gpu-1.lan:8880 (text-to-speech) stopped answering.",
    );
  });

  test("a dropped socket ends the call and nothing reconnects", async () => {
    const c = await dial();
    sock.open();
    sock.msg({ t: "connected" });
    sock.drop();
    expect(c.snapshot.ended?.reason).toBe("disconnected");
    expect(deps.openSocket).toHaveBeenCalledTimes(1);
    expect(audio.stopped).toBe(true);
  });

  test("the Home Free refusal code reads as such", async () => {
    const c = await dial();
    sock.drop(4409, "not over Home Free");
    expect(c.snapshot.ended?.reason).toBe("home_free");
  });

  test("messages after the end are ignored, and reset returns to idle", async () => {
    const c = await dial();
    sock.open();
    sock.msg({ t: "ended", reason: "idle" });
    sock.msg({ t: "caption", who: "hq", id: "late", text: "late" });
    expect(c.snapshot.captions).toHaveLength(0);
    c.reset();
    expect(c.snapshot.phase).toBe("idle");
  });
});

describe("spoken confirmation (#1397 P2)", () => {
  async function connected() {
    const c = await dial();
    sock.open();
    sock.msg({ t: "connected" });
    return c;
  }

  test("said puts a marker behind the queued speech; played goes back only when it is reached", async () => {
    await connected();
    sock.msg(new ArrayBuffer(8));
    sock.msg({ t: "said", id: "s1" });
    expect(audio.marks.map((m) => m.id)).toEqual(["s1"]);
    expect(sock.json().some((m) => m.t === "played")).toBe(false);
    audio.marks[0].onPlayed("s1");
    expect(sock.json().at(-1)).toEqual({ t: "played", id: "s1" });
  });

  test("no marker before connected, and no ack once the call ended", async () => {
    const c = await dial();
    sock.open();
    sock.msg({ t: "said", id: "early" });
    expect(audio.marks).toHaveLength(0);
    sock.msg({ t: "connected" });
    sock.msg({ t: "said", id: "s1" });
    const sent = sock.sent.length;
    c.end();
    audio.marks[0].onPlayed("s1");
    expect(sock.sent.slice(sent).map((x) => JSON.parse(x as string).t)).toEqual(["end"]);
  });

  test("a challenge names its cards and its window; [] clears it", async () => {
    const c = await connected();
    sock.msg({ t: "challenge", cards: ["p1", "p2"], expires_s: 20 });
    expect(c.snapshot.challenge).toEqual({ cards: ["p1", "p2"], expiresAt: 21_000 });
    sock.msg({ t: "challenge", cards: [] });
    expect(c.snapshot.challenge).toBeNull();
  });

  test("settled records the route's answer, then the end state after a 202, and re-reads the cards", async () => {
    const c = await connected();
    sock.msg({ t: "settled", card: "p1", ok: true, status: 202 });
    expect(c.snapshot.settled.p1).toEqual({ ok: true, status: 202 });
    expect(c.snapshot.cardSeq).toBe(1);
    sock.msg({ t: "settled", card: "p1", final: "started" });
    expect(c.snapshot.settled.p1).toEqual({ ok: true, status: 202, final: "started" });
    expect(c.snapshot.cardSeq).toBe(2);
  });

  test("a tap on a card says card_decided and closes its spoken window", async () => {
    const c = await connected();
    sock.msg({ t: "challenge", cards: ["p1"], expires_s: 20 });
    c.cardDecided("p1");
    expect(sock.json().at(-1)).toEqual({ t: "card_decided", id: "p1" });
    expect(c.snapshot.challenge).toBeNull();
  });

  test("What needs me? asks the server, only while connected", async () => {
    const c = await dial();
    sock.open();
    c.askNeedsYou();
    expect(sock.json().some((m) => m.t === "needs_you")).toBe(false);
    sock.msg({ t: "connected" });
    c.askNeedsYou();
    expect(sock.json().at(-1)).toEqual({ t: "needs_you" });
  });

  test("an ended call drops its challenge", async () => {
    const c = await connected();
    sock.msg({ t: "challenge", cards: ["p1"], expires_s: 20 });
    sock.drop();
    expect(c.snapshot.challenge).toBeNull();
  });
});

test("the live region says the phase, not the captions", async () => {
  const c = await dial();
  expect(phaseAnnouncement(c.snapshot, "General Hana Seo")).toBe("Calling HQ, General Hana Seo.");
  sock.open();
  sock.msg({ t: "connected" });
  sock.msg({ t: "caption", who: "hq", id: "h", text: "anything" });
  expect(phaseAnnouncement(c.snapshot, "General Hana Seo")).toBe("Connected to HQ, General Hana Seo.");
});
