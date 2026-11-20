import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { useNavigate } from "react-router-dom";
import { refreshRemoteShares, useRemoteShares } from "../../app/remoteShares";
import { ApiError, api } from "../../lib/api";
import { SESSIONS_PATH } from "../../lib/routes";
import {
  REMOTE_GONE,
  REMOTE_RETRY_MS,
  abortablePause as pause,
  remoteOfflineText,
  shortDate,
  TERMINAL_NOT_SHOWN,
} from "../../lib/federation";
import {
  REMOTE_FRAME_WAIT_S,
  SCREEN_MIN_INTERVAL_MS,
  SCREEN_ONLY,
  SCREEN_NOT_A_FRAME,
  SCREEN_ONLY_DETAIL,
  SCREEN_UNAVAILABLE,
  SCREEN_UNAVAILABLE_DETAIL,
  SCREEN_UPDATING,
  screenBadge,
  screenDeadline,
  screenHave,
  screenInit,
  screenLines,
  screenReduce,
  screenRuns,
  type ScreenEvent,
} from "../../lib/federationScreen";
import { ScreenTag } from "./PeerBadge";
import { RemoteFrame } from "./RemoteFrame";
import styles from "./Remote.module.css";
import { LeaveButton } from "./LeaveButton";
import { useLeaveShare } from "./useLeaveShare";

/** The server's `code` of a refused read (`{detail, code}`), when it sent one. */
function refusal(e: unknown): { status: number; code: string | null } {
  if (!(e instanceof ApiError)) return { status: 0, code: null };
  const code = (e.record as { code?: unknown } | null | undefined)?.code;
  return { status: e.status, code: typeof code === "string" ? code : null };
}

/** One row of the screen. Text the monospace face covers is a text node; any other character
 *  (a masked `█`, a box-drawing line) sits in a box one column wide, so the columns after it stay
 *  where the frame put them (`screenRuns`). Either way it is a string given to React: a text
 *  node, whatever it spells. Memoized on the line: a frame that changes one row redraws one. */
const ScreenLine = memo(function ScreenLine({ line }: { line: string }) {
  return (
    <div className={styles.screenLine}>
      {screenRuns(line).map((run, i) =>
        typeof run === "string" ? (
          run
        ) : (
          <span key={i} className={styles.screenCell}>
            {run.cell}
          </span>
        ),
      )}
    </div>
  );
});

/** A terminal another instance shares, as its screen (#2060).
 *
 *  **It is a picture of a screen as text, and nothing else.** The frame's lines are rendered as
 *  text nodes, one element per row, in a monospace block exactly `cols` characters wide (a
 *  character the face has no glyph for gets a box one column wide, see `ScreenLine`). Nothing
 *  in a frame is parsed: no markup, no link, no escape sequence, no colour. There is no terminal
 *  emulator here, no input, no caret and no key handler: the block can take focus only so that a
 *  keyboard can scroll it. A masked run arrives as `█` and is shown as it is; the pane has no
 *  reveal, no copy-unmasked and no hover affordance, because this instance never had the value.
 *
 *  **The loop** asks `…/frame?have=&wait=3` again and again. `have` is the fingerprint of the
 *  frame on screen (`screenHave`): with nothing on screen none is sent. What each answer means is
 *  `screenReduce`'s to say; the loop only carries answers to it.
 *
 *  **Every read is fenced** as in `RemotePane`: it carries the generation it started under and
 *  that generation's `AbortSignal`. The reducer bumps the generation whenever the frame is
 *  dropped (404, 503, the 30 s bound, another peer or grant), which aborts the read in flight
 *  here; an answer that arrives anyway is discarded there.
 *
 *  **Nothing stale, nothing kept.** The frame lives in this component's state and nowhere else:
 *  no storage, no retained list. A dimmed frame is only ever the last one this pane received in
 *  this view. */
export function RemoteScreenPane({ peerId, grantId }: { peerId: string; grantId: string }) {
  const target = `${peerId}/${grantId}`;
  const [state, dispatch] = useReducer(screenReduce, target, screenInit);
  // A new peer or grant on the SAME mounted route: clear before anything of the old one is
  // painted (a render-phase update; React re-renders at once, before the commit).
  if (state.target !== target) dispatch({ type: "open", target });

  const shared = useRemoteShares();
  const listed = shared.find((r) => r.peer_id === peerId && r.grant_id === grantId) ?? null;
  // What the list said of this share when it was last listed: the title and the peer's name
  // outlive the row (it leaves the list when the share ends), the screen does not.
  const [known, setKnown] = useState<{ target: string; title: string; peer: string } | null>(null);
  if (
    listed &&
    (known?.target !== target || known.title !== listed.title || known.peer !== listed.peer_name)
  )
    setKnown({ target, title: listed.title, peer: listed.peer_name });
  const facts = known?.target === target ? known : null;
  const peer = facts?.peer || "the other instance";
  const title = facts?.title || "Shared terminal";
  // Leave (#2201): the same action the conversation pane and Settings → Peers use. After a
  // proven leave this view has nothing to show: the app goes to the session list.
  const navigate = useNavigate();
  const leaving = useLeaveShare(useCallback(() => navigate(SESSIONS_PATH), [navigate]));

  const { generation, startAfterMs } = state;
  // The state this generation starts from, for the loop below (read after commit, never in
  // render).
  const latest = useRef(state);
  useLayoutEffect(() => {
    latest.current = state;
  });

  useEffect(() => {
    if (startAfterMs === null) return; // gone: nothing is asked again
    const ctl = new AbortController();
    const { signal } = ctl;
    // The loop's own copy of this generation's state, advanced by the SAME reducer with the same
    // events it dispatches. It is how the next read knows which frame is on screen without
    // waiting for a render: `have` is always `screenHave` of what the reducer made of the last
    // answer, and never a value the loop keeps for itself.
    let local = latest.current;
    const apply = (event: ScreenEvent) => {
      local = screenReduce(local, event);
      dispatch(event);
    };
    const run = async () => {
      await pause(startAfterMs, signal);
      while (!signal.aborted) {
        const asked = Date.now();
        let wait: number;
        try {
          const answer = await api.federationRemoteFrame(
            peerId,
            grantId,
            screenHave(local),
            REMOTE_FRAME_WAIT_S,
            signal,
          );
          if (signal.aborted) return;
          apply({ type: "answer", generation, answer, at: Date.now() });
          // An owner that answers at once (nothing held) must not be asked in a tight loop.
          wait = SCREEN_MIN_INTERVAL_MS - (Date.now() - asked);
        } catch (e) {
          if (signal.aborted) return;
          apply({ type: "failed", generation, ...refusal(e), at: Date.now() });
          wait = REMOTE_RETRY_MS;
        }
        // The answer dropped the frame: this generation is over, and the next one's loop asks.
        if (local.generation !== generation) return;
        await pause(wait, signal);
      }
    };
    void run();
    return () => ctl.abort();
  }, [peerId, grantId, generation, startAfterMs]);

  // The 30 s bound: tell the reducer the time when a dimmed frame's clock runs out.
  const deadline = screenDeadline(state);
  useEffect(() => {
    if (deadline === null) return;
    const timer = setTimeout(
      () => dispatch({ type: "tick", at: Math.max(Date.now(), deadline) }),
      Math.max(0, deadline - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [deadline]);

  // The share ended: the mirror was refreshed by the read that said so, so the list read that
  // follows no longer has the row.
  const gone = state.status === "gone";
  useEffect(() => {
    if (gone) void refreshRemoteShares();
  }, [gone]);

  const badge = screenBadge(state);
  const until = listed && !gone ? shortDate(listed.expires_at) : null;
  const common = {
    title,
    peer: facts?.peer ?? null,
    access: "screen only",
    tag: <ScreenTag />,
    word: badge.word,
    leave: (
      <>
        {listed && <LeaveButton share={listed} onLeave={leaving.open} />}
        {leaving.dialog}
      </>
    ),
  };

  if (gone) return <RemoteFrame {...common} empty={REMOTE_GONE} />;
  if (state.status === "offline")
    return <RemoteFrame {...common} until={until} empty={remoteOfflineText(peer)} />;

  const foot = (
    <p className={styles.viewOnly} data-testid="remote-screen-only">
      <b>{SCREEN_ONLY}</b> {SCREEN_ONLY_DETAIL}
    </p>
  );
  if (state.status === "unavailable")
    return (
      <RemoteFrame {...common} until={until} led={badge.led} foot={foot}>
        <div
          className={styles.empty}
          role="status"
          data-testid="remote-screen-unavailable"
          data-reason={state.reason ?? undefined}
        >
          <p>{SCREEN_UNAVAILABLE}</p>
          {/* Why, only when it is known: the owner's refusal is the renderer's limit, said in the
              dialog's own words; a frame this page could not draw is said as that. */}
          {state.reason === "screen" && (
            <p className={styles.emptySub} data-testid="remote-screen-why">
              {TERMINAL_NOT_SHOWN}
            </p>
          )}
          {state.reason === "frame" && (
            <p className={styles.emptySub} data-testid="remote-screen-why">
              {SCREEN_NOT_A_FRAME}
            </p>
          )}
          <p className={styles.emptySub}>{SCREEN_UNAVAILABLE_DETAIL}</p>
        </div>
      </RemoteFrame>
    );

  const { frame } = state;
  const marker =
    state.status === "loading" ? "Reading the screen…" : state.updating ? SCREEN_UPDATING : null;
  return (
    <RemoteFrame {...common} until={until} led={badge.led} foot={foot}>
      <div className={styles.screenBar} data-testid="remote-screen-bar">
        {marker ? (
          <span className={styles.screenMarker} role="status" data-testid="remote-screen-marker">
            <span className="nj-loader is-steps">
              <span className="nj-steps" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
            </span>
            <span>{marker}</span>
          </span>
        ) : (
          <span>Screen</span>
        )}
        {frame && (
          <span className={styles.screenDims} data-testid="remote-screen-dims">
            {frame.cols} × {frame.rows}
          </span>
        )}
      </div>
      {frame ? (
        // Focusable so a keyboard can scroll a frame wider or taller than the pane. It is a
        // group of text, not a textbox: no caret, no key handler, nothing editable.
        <div
          className={`${styles.screen} ${state.dimmed ? styles.screenDimmed : ""}`}
          role="group"
          aria-label={
            state.dimmed
              ? "The last screen, while a new one is read"
              : "The shared terminal screen, as text"
          }
          tabIndex={0}
          data-testid="remote-screen"
          data-dimmed={state.dimmed || undefined}
        >
          <div
            className={styles.screenText}
            style={{ "--screen-cols": frame.cols } as CSSProperties}
            data-cols={frame.cols}
            data-rows={frame.rows}
            data-testid="remote-screen-text"
          >
            {screenLines(frame).map((line, i) => (
              <ScreenLine key={i} line={line} />
            ))}
          </div>
        </div>
      ) : (
        <div className={styles.screenNone} />
      )}
    </RemoteFrame>
  );
}
