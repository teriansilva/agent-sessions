/** The call on screen (#1397 P1): the call panel (portalled to <body>, in HQ's place), the
 *  on-call bar (rendered where the shell puts it, a row of its own) and ONE polite live region
 *  that announces the phase — never every caption.
 *
 *  Focus: opening the panel moves focus into it; minimising moves it to the bar's Open button;
 *  closing the ended state returns it to the control that started the call. Escape inside the
 *  panel minimises it (the call goes on). */
import {
  ChevronDown,
  ChevronUp,
  Keyboard,
  Mic,
  MicOff,
  Phone,
  PhoneOff,
  RotateCcw,
  Subtitles,
  Volume2,
  X,
} from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useAskPanel } from "../ask/askPanel";
import { contactById } from "../hq/contacts";
import { HqPortrait } from "../hq/HqPortrait";
import b from "../ui/actionButton.module.css";
import { AudioSheet } from "./AudioSheet";
import { CallCards } from "./CallCards";
import { endedText, normalEnd, phaseAnnouncement } from "./callClient";
import { callActive, requestAskConversation, useCall } from "./callContext";
import s from "./call.module.css";

const STATE_WORD: Record<string, string> = {
  dialing: "Calling HQ…",
  listening: "Listening",
  thinking: "Checking",
  speaking: "HQ speaking",
  ended: "Call ended",
};

function fmt(ms: number): string {
  const t = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

function useTick(on: boolean, every = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const id = window.setInterval(() => setNow(Date.now()), every);
    return () => window.clearInterval(id);
  }, [on, every]);
  return now;
}

function Pips({ level, label, testId }: { level: number; label: string; testId: string }) {
  const lit = Math.min(5, Math.round(level * 5 * 4));
  return (
    <span className={s.lvl} data-testid={testId} data-level={lit}>
      {label}
      <span className={`nj-pips ${s.pips}`} aria-hidden="true">
        {[0, 1, 2, 3, 4].map((i) => (
          <i key={i} className={i < lit ? "on" : ""} />
        ))}
      </span>
    </span>
  );
}

function CallPanel() {
  const call = useCall();
  const { snap, caps, captionsOn } = call;
  const { openAsk } = useAskPanel();
  const contact = contactById(snap.contact);
  const live = callActive(snap);
  const now = useTick(live);
  // Choosing the microphone and speaker (#1670): only where the audio engine supports it.
  const devices = call.audioDevices();
  const [audioOpen, setAudioOpen] = useState(false);
  const audioId = useId();
  const [lv, setLv] = useState({ input: 0, output: 0 });
  useEffect(() => {
    if (snap.phase !== "connected") return;
    const id = window.setInterval(() => setLv(call.levels()), 150);
    return () => window.clearInterval(id);
  }, [snap.phase, call]);

  const panelRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    panelRef.current?.focus();
  }, []);
  // The newest caption stays in view; scrolling back up to read is the operator's to do.
  const capsRef = useRef<HTMLDivElement | null>(null);
  const lastCaption = snap.captions.at(-1)?.id;
  useEffect(() => {
    const el = capsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lastCaption, captionsOn]);

  const hosts = { stt: caps?.destinations.stt_host ?? "", tts: caps?.destinations.tts_host ?? "" };
  const dest = caps?.destinations;
  const word = (k: string | null | undefined) =>
    k === "loopback" ? "This machine" : k === "lan" ? "LAN" : k === "remote" ? "Remote" : "—";
  const speechKind =
    dest && dest.stt === dest.tts ? word(dest.stt) : `${word(dest?.stt)} / ${word(dest?.tts)}`;
  const speechHost =
    dest && dest.stt_host === dest.tts_host ? dest.stt_host : [dest?.stt_host, dest?.tts_host].filter(Boolean).join(" · ");

  const continueInText = () => {
    const cid = snap.conversationId;
    call.dismiss();
    if (cid) requestAskConversation(cid); // before the open: a console mounting now takes it
    openAsk();
  };
  const callAgain = (e: React.MouseEvent<HTMLButtonElement>) => {
    const cid = snap.conversationId;
    call.dismiss();
    call.start({ conversationId: cid, trigger: e.currentTarget });
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    e.preventDefault();
    if (live) call.setMinimized(true);
    else call.dismiss();
  };

  const elapsed = snap.connectedAt ? fmt(now - snap.connectedAt) : fmt(0);
  const dialing = snap.phase === "starting" || snap.phase === "dialing";
  const ended = snap.phase === "ended";

  return (
    <aside
      ref={panelRef}
      className={`nj-panel ${s.panel}`}
      role="region"
      aria-label="Call with HQ"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      data-focus-claim="" // a drawer closing as the call opens leaves focus here (useModalDrawer)
      data-testid="call-panel"
      data-phase={snap.phase}
      data-state={snap.serverState}
    >
      <div className={s.head}>
        <div className="nj-callsign">
          <span className="nj-notch" />
          <span className="nj-title">HQ</span>
          <span>//</span>
          <span className="nj-value">{dialing ? "Call" : ended ? "Call" : "On call"}</span>
          <span className="nj-meta">
            {dialing ? "Dialing" : ended ? "Link // down" : speechKind}
          </span>
        </div>
        {live ? (
          <button
            type="button"
            className={b.icon}
            aria-label="Minimise call"
            title="Minimise call"
            onClick={() => call.setMinimized(true)}
            data-testid="call-minimise"
          >
            <ChevronUp size={18} aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            className={b.icon}
            aria-label="Close"
            onClick={call.dismiss}
            data-testid="call-close"
          >
            <X size={18} aria-hidden="true" />
          </button>
        )}
      </div>

      {dialing ? (
        <div className={s.dialing}>
          {contact ? (
            <HqPortrait contact={contact} playing className={s.bigPortrait} testId="call-portrait" />
          ) : null}
          <p className={s.name}>{contact?.name ?? "HQ"}</p>
          <span className={s.state}>
            <span className="hud-led att" aria-hidden="true" />
            Calling HQ…
          </span>
          <p className={s.endedText}>Your microphone is ready but sends nothing until HQ picks up.</p>
          <span className={s.lvl}>
            Mic{" "}
            <span className="nj-stamp" data-testid="call-mic">
              Not sending
            </span>
          </span>
        </div>
      ) : (
        <div className={s.who}>
          {contact ? (
            <HqPortrait contact={contact} playing={live} className={s.smallPortrait} testId="call-portrait" />
          ) : null}
          <div className={s.whoText}>
            <p className={s.name}>{contact?.name ?? "HQ"}</p>
            <span className={s.timer} data-testid="call-timer">
              {ended ? `Ended at ${elapsed}` : elapsed}
            </span>
            {ended ? null : (
              <>
                <span className={s.state} data-testid="call-state">
                  <span
                    className={`hud-led ${snap.serverState === "speaking" || snap.serverState === "listening" ? "up" : "att"}`}
                    aria-hidden="true"
                  />
                  {STATE_WORD[snap.serverState] ?? snap.serverState}
                </span>
                <span className={s.levels}>
                  <Pips level={snap.muted ? 0 : lv.input} label="You" testId="call-level-you" />
                  <Pips level={lv.output} label="HQ" testId="call-level-hq" />
                </span>
                <span className={s.lvl}>
                  Mic{" "}
                  <span className="nj-stamp" data-testid="call-mic">
                    {snap.muted ? "Muted" : "Sending"}
                  </span>
                </span>
              </>
            )}
          </div>
        </div>
      )}

      {ended && snap.ended ? (
        <section
          className={`nj-panel is-sm ${normalEnd(snap.ended.reason) ? "" : "is-alert"} ${s.ended}`}
          data-testid="call-ended"
          data-reason={snap.ended.reason}
        >
          <div className="nj-face">
            <span className="nj-rail" />
            <p className={`${s.endedTitle} ${normalEnd(snap.ended.reason) ? "" : s.down}`}>
              {normalEnd(snap.ended.reason) ? "Call ended" : "▲ Call dropped"}
            </p>
            <p className={s.endedText} data-testid="call-ended-text">
              {endedText(snap.ended, hosts)}
            </p>
            <div className={s.stamps}>
              <span className="nj-stamp">Mic off</span>
              <span className="nj-stamp">Playback stopped</span>
            </div>
            <div className={s.acts}>
              <button
                type="button"
                className={b.primary}
                onClick={callAgain}
                data-testid="call-again"
              >
                <RotateCcw size={15} aria-hidden="true" /> Call again
              </button>
              <button
                type="button"
                className={b.ghost}
                onClick={continueInText}
                data-testid="call-continue-text"
              >
                <Keyboard size={15} aria-hidden="true" /> Continue in text
              </button>
            </div>
            <p className={s.note}>Calling again is your tap: the microphone is never reopened on its own.</p>
          </div>
        </section>
      ) : null}

      {captionsOn && (snap.captions.length > 0 || snap.phase === "connected") ? (
        <div ref={capsRef} className={s.captions} data-testid="call-captions" aria-label="Captions">
          {snap.captions.length === 0 ? (
            <p className={s.empty}>Talk any time — HQ stops and listens.</p>
          ) : (
            snap.captions.map((c) => (
              <p
                key={c.id}
                className={`${s.cap} ${c.status === "queued" ? s.capQueued : ""} ${c.status === "cut" ? s.capCut : ""}`}
                data-testid="call-caption"
                data-who={c.who}
                data-status={c.status ?? "heard"}
              >
                <span className={s.spk}>{c.who === "you" ? "Commander" : contact?.name ?? "HQ"}</span>
                {c.text}
              </p>
            ))
          )}
        </div>
      ) : null}

      {!dialing ? <CallCards cid={snap.conversationId} seq={snap.turnSeq + snap.cardSeq} /> : null}

      {snap.phase === "connected" ? (
        <>
          <p className={s.note}>
            Talk any time — HQ stops and listens. Say “confirm” when HQ reads a card back; tapping always
            works.
          </p>
          <div className={s.needsRow}>
            <button
              type="button"
              className={`nj-chip ${s.needsChip}`}
              disabled={snap.serverState === "thinking"}
              onClick={call.askNeedsYou}
              data-testid="call-needs-you"
            >
              What needs me?
            </button>
            <span className={s.voiceNote}>Permission prompts and menus stay on screen.</span>
          </div>
        </>
      ) : null}
      {dest ? (
        <p className={s.dest} data-testid="call-destinations">
          <span>
            Speech <b>{speechHost || "—"} · {speechKind}</b>
          </span>
          <span>
            HQ AI <b>{dest.ai_host || "—"} · {word(dest.ai)}</b>
          </span>
          <span>
            Audio <b>never stored</b>
          </span>
        </p>
      ) : null}

      {live && audioOpen && devices && snap.phase === "connected" ? (
        <AudioSheet api={devices} id={audioId} />
      ) : null}
      {live ? (
        <div className={s.controls}>
          <div className={s.ctl}>
            <button
              type="button"
              className={`${b.ghost} ${s.ctlBtn}`}
              aria-pressed={snap.muted}
              aria-label={snap.muted ? "Unmute" : "Mute"}
              disabled={dialing}
              onClick={() => call.setMuted(!snap.muted)}
              data-testid="call-mute"
            >
              {snap.muted ? <MicOff size={20} aria-hidden="true" /> : <Mic size={20} aria-hidden="true" />}
            </button>
            <span className={s.ctlLabel} aria-hidden="true">
              {snap.muted ? "Unmute" : "Mute"}
            </span>
          </div>
          <div className={s.ctl}>
            <button
              type="button"
              className={`${b.ghost} ${s.ctlBtn}`}
              aria-label="Type instead"
              onClick={continueInTextLive(call, openAsk, snap.conversationId)}
              data-testid="call-type"
            >
              <Keyboard size={20} aria-hidden="true" />
            </button>
            <span className={s.ctlLabel} aria-hidden="true">
              Type
            </span>
          </div>
          <div className={s.ctl}>
            <button
              type="button"
              className={`${b.ghost} ${s.ctlBtn}`}
              aria-pressed={captionsOn}
              aria-label="Captions"
              onClick={() => call.setCaptionsOn(!captionsOn)}
              data-testid="call-captions-toggle"
            >
              <Subtitles size={20} aria-hidden="true" />
            </button>
            <span className={s.ctlLabel} aria-hidden="true">
              Captions
            </span>
          </div>
          <div className={s.ctl}>
            <button
              type="button"
              className={`${b.ghost} ${s.ctlBtn}`}
              aria-expanded={audioOpen}
              aria-controls={audioId}
              aria-label="Audio: microphone and speaker"
              disabled={!devices || snap.phase !== "connected"}
              onClick={() => setAudioOpen(!audioOpen)}
              data-testid="call-audio"
            >
              <Volume2 size={20} aria-hidden="true" />
            </button>
            <span className={s.ctlLabel} aria-hidden="true">
              Audio
            </span>
          </div>
          <div className={s.ctl}>
            <button
              type="button"
              className={`${b.dangerFilled} ${s.ctlBtn}`}
              aria-label={dialing ? "Cancel call" : "End call"}
              onClick={call.end}
              data-testid="call-end"
            >
              <PhoneOff size={20} aria-hidden="true" />
            </button>
            <span className={s.ctlLabel} aria-hidden="true">
              {dialing ? "Cancel" : "End"}
            </span>
          </div>
        </div>
      ) : null}
    </aside>
  );
}

/** "Type" during a call: HQ's panel opens on the call's conversation; the call goes on minimised. */
function continueInTextLive(
  call: ReturnType<typeof useCall>,
  openAsk: () => void,
  cid: string | null,
) {
  return () => {
    call.setMinimized(true);
    if (cid) requestAskConversation(cid);
    openAsk();
  };
}

function CallBar() {
  const call = useCall();
  const { snap } = call;
  const contact = contactById(snap.contact);
  const now = useTick(snap.phase === "connected");
  const openRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    openRef.current?.focus();
  }, []);
  const last = [...snap.captions].reverse().find((c) => c.who === "hq");
  const word = STATE_WORD[snap.serverState] ?? "";
  return (
    <div className={s.bar} role="region" aria-label="Call with HQ" data-testid="call-bar">
      {contact ? (
        <HqPortrait contact={contact} playing={false} className={s.barPortrait} testId="call-bar-portrait" />
      ) : (
        <Phone size={18} aria-hidden="true" />
      )}
      <div className={s.barText}>
        <span className={s.barL1}>
          <span className={`hud-led ${snap.phase === "connected" ? "up" : "att"}`} aria-hidden="true" />
          {contact ? contact.name.replace(/^General /, "Gen. ") : "HQ"} ·{" "}
          {snap.connectedAt ? fmt(now - snap.connectedAt) : "Calling"}
          <span className={s.barHideNarrow}>&nbsp;· On call</span>
        </span>
        <span className={s.barL2}>
          {word}
          {last ? ` · HQ: “${last.text}”` : ""}
        </span>
      </div>
      <button
        type="button"
        className={`${b.icon} ${s.barBtn}`}
        aria-pressed={snap.muted}
        aria-label={snap.muted ? "Unmute" : "Mute"}
        disabled={snap.phase !== "connected"}
        onClick={() => call.setMuted(!snap.muted)}
        data-testid="call-bar-mute"
      >
        {snap.muted ? <MicOff size={18} aria-hidden="true" /> : <Mic size={18} aria-hidden="true" />}
      </button>
      <button
        ref={openRef}
        type="button"
        className={`${b.icon} ${s.barBtn}`}
        aria-label="Open the call"
        onClick={() => call.setMinimized(false)}
        data-testid="call-bar-open"
      >
        <ChevronDown size={18} aria-hidden="true" />
      </button>
      <button
        type="button"
        className={`${b.dangerFilled} ${b.icon} ${s.barBtn}`}
        aria-label="End call"
        onClick={call.end}
        data-testid="call-bar-end"
      >
        <PhoneOff size={18} aria-hidden="true" />
      </button>
    </div>
  );
}

/** The shell renders this once, in the call bar's row. */
export function CallLayer() {
  const { snap, minimized } = useCall();
  const contact = contactById(snap.contact);
  const announce = phaseAnnouncement(snap, contact?.name ?? "");
  const showPanel = snap.phase !== "idle" && !(minimized && callActive(snap));
  const showBar = minimized && callActive(snap);
  return (
    <>
      {/* Mounted on every page (a live region must exist before it speaks), so NOT
          role="status": a page's own status message stays the one `getByRole("status")` finds. */}
      <p className="sr-only" aria-live="polite" aria-atomic="true" data-testid="call-live">
        {announce}
      </p>
      {showBar ? <CallBar /> : null}
      {showPanel ? createPortal(<CallPanel />, document.body) : null}
    </>
  );
}
