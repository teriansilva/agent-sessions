/** VIEW SCREEN — the live screen, read-only, from the thread (#894, Phase 6 of #840).
 *
 * #840's central promise is that *"jumping into the terminal stays available at every moment; it
 * stops being required"*. Seeing what an agent is showing is the most ordinary reason to go and
 * open a terminal, so this is the last routine thing that required one.
 *
 * **Read-only on purpose, and the reason is not tidiness.** Attaching a terminal marks the viewer
 * BUSY, and `actuator._viewer_busy` suppresses autonomous delivery for `VIEWER_RECENT_S` after
 * any recent output — so "let me just look" would silently pause the orchestrator on the very
 * mission the operator is checking on. Peeking must not have side effects.
 *
 * So it goes through `GET /api/pulse/evidence/{id}?kind=screen`, which is the route the
 * orchestrator's own evidence already uses: a server-side read of the scrollback ring. **No
 * lease is taken, no socket is opened, no width is negotiated.** Ownership is keyed on
 * `(fp, tab_id)`; a viewer that took a lease would make the console a second writer on a session
 * somebody may be attached to, which is the failure the single-writer lock exists to prevent.
 *
 * Never cached: the response's whole contract is "what the session shows RIGHT NOW", and the
 * route sends `no-store` for that reason. A frozen screen is worse than no screen — it is a
 * screen the operator will act on.
 */
import { Send } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api, ApiError } from "../../lib/api";
import type { Evidence } from "../../types/api";

import action from "../ui/actionButton.module.css";
import type { DraftEdit } from "./draftDirection";
import styles from "./mission.module.css";

export function MissionScreen({
  missionId,
  sessionKey,
  role,
  onGone,
  prefill,
  onDraftReplaced,
}: {
  /** #983 P3: an AI-drafted direction opened by Edit, for THIS session. Its text becomes the
   *  composer's, and the next send REPLACES the draft: the server closes the draft before it
   *  records the relay, so what goes out is the operator's own message. */
  prefill?: DraftEdit | null;
  /** The server has closed that draft (the send replaced it, or tried to). The console re-reads
   *  its decisions, which is what takes the card away. */
  onDraftReplaced?: (actionId: string) => void;
  missionId: string;
  sessionKey: string;
  /** The session's role on the mission, when it has one. Shown beside the key, because on a
   *  mission holding several "primary" is the thing an operator actually reasons about. */
  role?: string | null;
  /** The mission no longer holds this session — the server said so, at request time. The
   *  console re-reads the roster, which is what removes this block; leaving it on screen would
   *  leave a SEND box pointing at an agent that is now somebody else's (#903 review 3,
   *  finding 1). */
  onGone?: () => void;
}) {
  const [screen, setScreen] = useState<Evidence | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [relay, setRelay] = useState("");
  const [sending, setSending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  /** The AI draft the next send replaces (#983 P3), or null for an ordinary relay. */
  const [replaces, setReplaces] = useState<string | null>(null);
  const [seenPrefill, setSeenPrefill] = useState<number | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  /** WHAT THE COMPOSER IS HOLDING RIGHT NOW (review 4887, finding 3). A send captures it, and a
   *  response may only touch the composer while it is still the same: Edit can adopt another draft
   *  while a send is out, and clearing that one's text — or its replacement, which would turn the
   *  next Send into an ordinary relay leaving a live proposal behind — is not this answer's to do.
   *
   *  State, so it is set the same way the adoption below sets everything else, mirrored into a ref
   *  because the callbacks that ask are closures over an older render. The mirror is written in an
   *  effect: a ref may not be touched during render, and every send starts from an event, which is
   *  always after the commit that armed it. */
  const [armed, setArmed] = useState(0);
  const armedNow = useRef(0);
  useEffect(() => {
    armedNow.current = armed;
  }, [armed]);
  // ADOPTED DURING RENDER, once per Edit: a second Edit of the same draft is a new nonce, so text
  // the operator cleared comes back when they ask for it again.
  if (prefill && prefill.nonce !== seenPrefill) {
    setSeenPrefill(prefill.nonce);
    setArmed((n) => n + 1);
    setRelay(prefill.text);
    setReplaces(prefill.actionId);
    setNote(null);
  }
  // WHILE IT HOLDS A DRAFT THE FIELD GROWS WITH ITS TEXT, up to the CSS `max-height`, after which it
  // scrolls. Measured before paint so it never flashes at the wrong height. The one-line relay keeps
  // its CSS height.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    if (!replaces) {
      el.style.height = "";
      return;
    }
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  }, [relay, replaces]);
  useEffect(() => {
    if (seenPrefill === null) return;
    // After the details band and the Context section have opened in this same commit.
    const frame = requestAnimationFrame(() => {
      const el = input.current;
      if (!el) return;
      el.focus();
      // The caret at the START, so the draft reads from its first line rather than opening
      // scrolled to its last.
      el.setSelectionRange(0, 0);
      el.scrollTop = 0;
      el.scrollIntoView({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
  }, [seenPrefill]);

  const look = useCallback(async () => {
    setBusy(true);
    setError(null);
    // CLOSED BEFORE THE READ, not left standing beside the error (#903 review 3, finding 6).
    // This component's own contract is that a frozen screen is worse than none: what it shows is
    // labelled "what this agent is doing RIGHT NOW", and a failed refresh that leaves the last
    // successful read on screen keeps making that claim about output of unknown age. The
    // operator pressed REFRESH because they wanted to know the current state; the honest answer
    // to "we could not find out" is to stop showing the old one.
    setScreen(null);
    setOpen(false);
    try {
      // MISSION-SCOPED. The membership is checked server-side at request time, so a session this
      // mission no longer holds cannot answer with its screen.
      const r = await api.missionScreen(missionId, sessionKey);
      setScreen(r);
      setOpen(true);
    } catch (err) {
      // BY KIND, not by payload — and never conflated with an empty screen, which is a
      // different fact the response itself reports as `available: false`.
      if (err instanceof ApiError && err.status === 409) {
        setError("This mission no longer holds that session.");
        onGone?.();
      } else {
        setError("The screen could not be read.");
      }
    } finally {
      setBusy(false);
    }
  }, [missionId, sessionKey, onGone]);

  return (
    <div
      className={styles.screenBlock}
      data-testid="mission-screen"
      data-session={sessionKey}
      aria-label={`Session ${sessionKey}`}
    >
      {/* WHICH SESSION THIS BLOCK IS (#903 review 2, finding 2). A mission can hold several, and
          the roster printed once above with identical control blocks below it left the operator
          unable to tell which SEND box types into which live agent — which is not a cosmetic
          problem when the thing on the other end is an agent with permission bypass. The heading
          names the target, and every control's accessible name carries it too, so the keyboard
          and screen-reader paths are not a second, unlabelled surface. */}
      <div className={styles.screenTarget} data-testid="screen-target">
        {sessionKey}
        {role ? <span className={styles.supGate}>{role}</span> : null}
      </div>
      <div className={styles.screenBar}>
        <button
          type="button"
          className={styles.missionQBtn}
          disabled={busy}
          onClick={() => void look()}
          aria-label={`${open ? "Refresh" : "View"} the screen of ${sessionKey}`}
          data-testid="view-screen"
        >
          {busy ? "…" : open ? "REFRESH SCREEN" : "VIEW SCREEN"}
        </button>
        {open ? (
          <button
            type="button"
            className={styles.missionQBtn}
            onClick={() => setOpen(false)}
            aria-label={`Hide the screen of ${sessionKey}`}
            data-testid="hide-screen"
          >
            HIDE
          </button>
        ) : null}
        <span className={styles.screenNote}>
          Read-only. Looking does not attach, and does not pause the
          follow-through.
        </span>
      </div>
      {error ? (
        <div className={styles.objStale} data-testid="screen-error">
          {error}
        </div>
      ) : null}
      {/* RELAY — the operator's own words, into this session (#894, #840 §9).
          Beside the screen deliberately: reading what an agent is showing and answering it are
          one act, and separating them is what sends people back to the terminal. */}
      {/* STACKED WHILE IT HOLDS A DRAFT (#983 P3): the note, a full-width field that grows, then
          Cancel and Send on one row. The Context column is narrow, and a field sharing its row with
          SEND wrapped a draft at about twenty characters. The ordinary relay keeps its one line. */}
      <form
        className={replaces ? styles.relayDraftForm : styles.relayRow}
        onSubmit={(e) => {
          e.preventDefault();
          const body = relay.trim();
          if (!body || sending) return;
          setSending(true);
          setNote(null);
          // Captured for THIS send: the server closes the draft before the relay is recorded, so
          // whatever it answers about the draft, the next send is an ordinary relay.
          const draft = replaces;
          // …and what the composer held when it went out. `mine()` is false once Edit has adopted
          // something else, which is the only thing that may not be overwritten by a late answer.
          const sendGen = armedNow.current;
          const mine = () => armedNow.current === sendGen;
          const settleDraft = (replaced: unknown) => {
            if (!draft || typeof replaced !== "boolean") return;
            if (mine()) setReplaces(null);
            // The DURABLE half, announced whoever holds the composer now: the server closed that
            // draft, so its card has to go.
            if (replaced) onDraftReplaced?.(draft);
          };
          void api
            .relayToSession(missionId, sessionKey, body, draft ?? undefined)
            .then((r) => {
              settleDraft(r.draft_replaced);
              // The server's own verdict, by NAME. "delivered" is the only one that means the
              // bytes landed; the rest are different facts with different fixes, and flattening
              // them to "sent" is the lie this whole feature is built to avoid.
              //
              // …and the DRAFT SURVIVES ANYTHING BUT DELIVERY (#903 review 2, finding 1). A
              // refusal — "a viewer is attached", "the session never went quiet" — sends zero
              // bytes and is precisely the case the operator retries, so clearing the box on it
              // deletes an instruction they may have spent a minute writing. Cleared only when
              // the bytes actually landed.
              if (r.state === "delivered" && mine()) setRelay("");
              setNote(
                r.state === "delivered"
                  ? "Sent."
                  : `Not sent — ${r.detail || r.state}.`,
              );
            })
            .catch((err: unknown) => {
              // AMBIGUOUS IS NOT REFUSED (#903 review 4, finding 3). "Not sent" over a delivery
              // that may already be in the pty invites a retry of bytes the agent might have —
              // the one outcome this whole path is careful about. The server marks that case
              // explicitly; without the marker the two 5xx bodies are indistinguishable prose.
              //
              // The draft is kept either way, which is right for both: a refusal is retried, and
              // an ambiguous one is decided by the operator after they have looked.
              const body =
                err instanceof ApiError
                  ? (err.record as
                      | { state?: string; draft_replaced?: boolean }
                      | undefined)
                  : undefined;
              // A draft that was no longer waiting is refused with nothing sent, and the text
              // stays. Either answer about the draft ends the replacement.
              settleDraft(body?.draft_replaced);
              if (body?.state === "indeterminate") {
                setNote(
                  `Delivery uncertain — ${
                    err instanceof ApiError ? err.message : "check the session"
                  }`,
                );
                return;
              }
              setNote(
                err instanceof ApiError && err.message
                  ? `Not sent — ${err.message}.`
                  : "That could not be sent.",
              );
            })
            .finally(() => setSending(false));
        }}
      >
        {replaces ? (
          <p className={styles.relayDraft} data-testid="relay-draft-edit">
            Your edit of the AI&apos;s draft. Sending it replaces the draft and
            goes to this session as your own message.
          </p>
        ) : null}
        {/* A TEXTAREA (#983 P3): a prefilled AI draft can hold line breaks, and an `<input>` drops
            them, so the operator would send something other than what they read. Enter still
            sends, except while editing a draft, where it adds a line. */}
        <textarea
          ref={input}
          className={
            replaces
              ? `${styles.relayInput} ${styles.relayDraftInput}`
              : styles.relayInput
          }
          rows={replaces ? 4 : 1}
          value={relay}
          disabled={sending}
          onChange={(e) => setRelay(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !replaces &&
              !e.nativeEvent.isComposing
            ) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="Type to this session…"
          aria-label={`Type to ${sessionKey}`}
          data-testid="relay-input"
        />
        {replaces ? (
          // The dialog's pair (#983 P2): ghost Cancel, then the primary Send.
          <div className={styles.relayDraftActions}>
            <button
              type="button"
              className={action.ghost}
              disabled={sending}
              onClick={() => {
                // A new generation: whatever is in flight no longer owns this composer.
                setArmed((n) => n + 1);
                setReplaces(null);
                setRelay("");
              }}
              data-testid="relay-draft-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              className={action.primary}
              disabled={sending || !relay.trim()}
              aria-label={`Send to ${sessionKey}`}
              data-testid="relay-send"
            >
              <Send size={14} aria-hidden="true" />
              {sending ? "Sending…" : "Send"}
            </button>
          </div>
        ) : (
          <button
            type="submit"
            className={styles.missionQBtn}
            disabled={sending || !relay.trim()}
            aria-label={`Send to ${sessionKey}`}
            data-testid="relay-send"
          >
            {sending ? "…" : "SEND"}
          </button>
        )}
      </form>
      {note ? (
        <div className={styles.objReason} data-testid="relay-note">
          {note}
        </div>
      ) : null}

      {open && screen ? (
        screen.available ? (
          // Terminal output as TEXT in a `pre`. React escapes it; there is no
          // `dangerouslySetInnerHTML` anywhere in these components, and a screen is the one
          // place where somebody else's bytes are being rendered verbatim.
          <pre className={styles.screen} data-testid="screen-text">
            {screen.text}
          </pre>
        ) : (
          <div className={styles.objReason} data-testid="screen-unavailable">
            Nothing to show — this session has produced no output yet.
          </div>
        )
      ) : null}
    </div>
  );
}
