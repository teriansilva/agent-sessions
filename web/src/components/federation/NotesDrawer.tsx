import { X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type Dispatch } from "react";
import { createPortal } from "react-dom";
import { ApiError, api } from "../../lib/api";
import { draftKey, shortTime } from "../../lib/federation";
import {
  NOTE_CHANGE_UNCONFIRMED,
  NOTE_DELETED,
  NOTE_RATE,
  NOTE_UNCONFIRMED,
  NOTES_ENDED_OWNER,
  NOTES_ENDED_PEER,
  NOTES_POLL_MS,
  cursorOf,
  noteActions,
  noteCounter,
  noteFailure,
  noteFits,
  noteMarks,
  notesLine,
  notesOffline,
  operatorOf,
  type Note,
  type NoteThread,
  type NotesEvent,
  type PendingNote,
} from "../../lib/federationTogether";
import { useIsMobile } from "../../lib/useIsMobile";
import { Led } from "../hud/Meters";
import { useFocusContainment } from "../pulse/useModalDrawer";
import btn from "../ui/actionButton.module.css";
import { PeerBadge } from "./PeerBadge";
import styles from "./Together.module.css";
import { useAnswerFence } from "./useAnswerFence";

const statusOf = (e: unknown) => (e instanceof ApiError ? e.status : 0);
const codeOf = (e: unknown) =>
  e instanceof ApiError ? (e.record as { code?: unknown } | null | undefined)?.code : undefined;
const uuid = () => crypto.randomUUID();
const PAGES_MAX = 30;
const seconds = () => Date.now() / 1000;

/** The notes between the two operators of ONE grant (#2062): a drawer on a wide screen, a sheet
 *  on a phone. Both sides use it; `side` picks the routes and what may be done to a note.
 *
 *  **A note is read by a person.** Its text is a text node here and nothing else: not Markdown,
 *  not a link, not HTML. There is no control that sends a note to an agent, quotes it into a
 *  composer or forwards it anywhere; copying is the operating system's copy.
 *
 *  **Keyed by grant.** The thread, its draft, its change cursor and its pending writes are the
 *  parent's state under this grant id (`notesReduce`), and every event this component raises
 *  names that grant. Another grant's drawer is another instance of this component (`key`), so
 *  nothing typed for one peer is in the box, or can be sent, after switching.
 *
 *  **Memory only.** Nothing here reads or writes local or session storage. The receiving side
 *  reads through the channel with the change cursor and keeps what it read in the parent's
 *  state; offline or after the share ended it shows no note.
 *
 *  **One read at a time, and an older answer never lands over a newer one.** The poll and the
 *  re-read after an unconfirmed write share one in-flight read (`pump`); the reducer drops a
 *  page that was asked from a cursor the thread has left.
 *
 *  **One submission, one key.** A note is sent once per press. No answer is *not confirmed*,
 *  with Retry under the SAME key (the owner returns the note it has or saves it once) and the
 *  text still in the box. A refusal that says so (the hourly bound) is a confirmed failure. */
export function NotesDrawer({
  side,
  grantId,
  peerId,
  other,
  thread,
  dispatch,
  epoch,
  anchor,
  onClose,
  returnFocusTo,
  stillCurrent,
}: {
  side: "owner" | "peer";
  grantId: string;
  /** The owning instance, on the receiving side. */
  peerId?: string;
  /** The other instance's name, as this one shows it. */
  other: string;
  thread: NoteThread;
  dispatch: Dispatch<NotesEvent>;
  /** Bumped by the parent whenever its pane cleared: an answer from before it is dropped. */
  epoch: number;
  /** The pane the drawer sits over on a wide screen. */
  anchor: HTMLElement | null;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  /** Whether the PARENT's pane is still the one a write started under (`epoch`). A write's
   *  answer outlives this drawer: it lands in its own grant's thread after the drawer closed or
   *  another thread opened, and is dropped only once the pane cleared or left. */
  stillCurrent: (epoch: number) => boolean;
}) {
  const sheet = useIsMobile();
  const panelRef = useRef<HTMLElement | null>(null);
  const boxRef = useRef<HTMLTextAreaElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  // What an answer checks before it touches anything: the epoch of the LATEST commit (written
  // after every render, read only by handlers), and whether this drawer is still mounted for this
  // grant: a fence armed in an effect BODY, so StrictMode's replay arms it again (#2061's
  // `useAnswerFence`; an `alive` ref cleared by the replay's cleanup would drop every answer).
  const now_ = useRef({ epoch });
  const seq = useRef(thread.seq);
  useLayoutEffect(() => {
    now_.current.epoch = epoch;
    seq.current = thread.seq;
  });
  const ticket = useAnswerFence(grantId);
  /** Whether this drawer is mounted right now. */
  const mounted = useCallback(() => ticket()(), [ticket]);
  /** Whether an answer that started under `e` may still touch anything. */
  const current = useCallback((e: number) => mounted() && now_.current.epoch === e, [mounted]);

  const marked = useRef(0);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // ---- reading -------------------------------------------------------------------------------
  const read = useCallback(
    async (signal: AbortSignal) => {
      const e = now_.current.epoch;
      try {
        let newest = 0;
        if (side === "owner") {
          const answer = await api.federationNotes(grantId, signal);
          if (signal.aborted || !current(e)) return;
          dispatch({
            type: "thread",
            grantId,
            notes: answer?.notes,
            ended: answer?.ended === true,
            seq: answer?.seq,
          });
          if (answer?.ended === true) return;
          newest = cursorOf(answer?.seq, 0);
        } else {
          if (!peerId) return;
          // What changed since the cursor; a `reset` is the thread again, page by page.
          let after = seq.current;
          for (let i = 0; i < PAGES_MAX; i++) {
            let page = (await api.federationRemoteNotes(peerId, grantId, after, null, signal)) as Record<
              string,
              unknown
            > | null;
            if (signal.aborted || !current(e)) return;
            if (page?.reset === true) {
              const changes = Array.isArray(page.changes) ? [...page.changes] : [];
              for (let j = 0; j < PAGES_MAX && page?.more === true && typeof page.next === "string"; j++) {
                page = (await api.federationRemoteNotes(peerId, grantId, after, page.next, signal)) as Record<
                  string,
                  unknown
                > | null;
                if (signal.aborted || !current(e)) return;
                if (Array.isArray(page?.changes)) changes.push(...page.changes);
              }
              page = { ...page, reset: true, changes };
            }
            // `after`: the cursor this page was asked from. The reducer applies it only while
            // the thread still stands there, and this loop goes on only if it did.
            if (seq.current !== after) return;
            dispatch({ type: "page", grantId, page, after });
            // Not a cursor (`NaN`, `1e300`, …): nothing to ask after, nothing to mark read.
            newest = cursorOf(page?.seq, 0);
            // The reducer's own arithmetic, so the next read (which may start before React
            // rendered this one) asks from where the thread now stands.
            seq.current = Math.max(page?.reset === true ? 0 : after, newest);
            if (page?.more !== true || page.reset === true || newest <= after) break;
            after = newest;
          }
        }
        // Read: the other side's bell row for this thread goes. Not a change to the thread.
        if (newest > marked.current) {
          const mark =
            side === "owner"
              ? api.readFederationNotes(grantId, newest)
              : api.readFederationRemoteNotes(peerId ?? "", grantId, newest);
          const before = marked.current;
          marked.current = newest;
          // A mark that got no answer is asked again with the next read (it only ever says "read
          // up to here", so a repeat changes nothing).
          void Promise.resolve(mark).catch(() => {
            if (marked.current === newest) marked.current = before;
          });
        }
      } catch (err) {
        if (signal.aborted || !current(e)) return;
        dispatch({ type: "failed", grantId, status: statusOf(err) });
      }
    },
    [side, grantId, peerId, dispatch, current],
  );

  const stopped = thread.status === "gone" || thread.status === "ended";
  // The live read loop's signal: what a write that was not confirmed reads the thread with.
  const reading = useRef<AbortSignal | null>(null);
  // ONE READ PER THREAD AT A TIME. A tick, or a re-read after a write, that comes while a read is
  // in flight starts nothing: it is remembered, and exactly one read follows when that one ends.
  // Two reads in flight could land out of order, and the older one would put back what the
  // newer one removed (the reducer refuses that too: `threadReduce`, `page`). A run that was
  // aborted (the drawer closed, the pane cleared) never holds up the next run's first read.
  const flight = useRef<{ signal: AbortSignal; again: boolean } | null>(null);
  const pump = useCallback(
    (signal: AbortSignal) => {
      if (signal.aborted) return;
      const held = flight.current;
      if (held && held.signal === signal) {
        held.again = true;
        return;
      }
      const mine = { signal, again: false };
      flight.current = mine;
      void (async () => {
        do {
          mine.again = false;
          await read(signal);
        } while (mine.again && !signal.aborted && flight.current === mine);
        if (flight.current === mine) flight.current = null;
      })();
    },
    [read],
  );
  /** After a write whose outcome is not known: ask what the thread holds NOW. A read; the
   *  write itself is never sent again from here. */
  const reread = useCallback(() => {
    const signal = reading.current;
    if (signal) pump(signal);
  }, [pump]);
  useEffect(() => {
    if (stopped) return; // final: nothing is asked again
    const ctl = new AbortController();
    reading.current = ctl.signal;
    pump(ctl.signal);
    const timer = setInterval(() => pump(ctl.signal), NOTES_POLL_MS);
    return () => {
      ctl.abort();
      clearInterval(timer);
    };
  }, [pump, stopped, epoch]);

  // ---- the surface ---------------------------------------------------------------------------
  const writable = thread.status === "live" || thread.status === "loading";
  useEffect(() => {
    (boxRef.current ?? closeRef.current)?.focus({ preventScroll: true });
  }, []);
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useFocusContainment({ active: sheet, panelRef });
  const close = useCallback(() => {
    dispatch({ type: "close", grantId });
    onClose();
  }, [dispatch, grantId, onClose]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // The sheet owns the screen; the drawer only when focus is in it.
      if (!sheet && !panelRef.current?.contains(document.activeElement)) return;
      e.preventDefault();
      if (editing) setEditing(null);
      else close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sheet, close, editing]);

  const [place, setPlace] = useState<{ top: number; left: number; height: number } | null>(null);
  useLayoutEffect(() => {
    if (sheet || !anchor) return;
    const measure = () => {
      const r = anchor.getBoundingClientRect();
      const width = Math.min(340, window.innerWidth);
      setPlace({ top: r.top, left: Math.max(0, r.right - width), height: r.height });
    };
    measure();
    window.addEventListener("resize", measure);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    ro?.observe(anchor);
    return () => {
      window.removeEventListener("resize", measure);
      ro?.disconnect();
    };
  }, [sheet, anchor]);

  // ---- writing -------------------------------------------------------------------------------
  const sending = thread.pending.some((p) => p.state === "sending");
  const send = async (retry?: PendingNote) => {
    const text = retry ? retry.text : thread.draft;
    if (!writable || sending || !noteFits(text)) return;
    // ONE key per submission: the held one while the text is the same, a new one otherwise.
    const client_key = retry ? retry.client_key : draftKey(thread.held, text, uuid).key;
    const e = now_.current.epoch;
    setError(null);
    dispatch({ type: "submit", grantId, client_key, text, now: seconds() });
    try {
      const answer =
        side === "owner"
          ? await api.addFederationNote(grantId, { client_key, text })
          : await api.addFederationRemoteNote(peerId ?? "", grantId, { client_key, text });
      if (!stillCurrent(e)) return;
      // The add's change number goes with it: the reducer inserts the note only if no read
      // has consumed that change yet (THE ORDER RULE in `federationTogether.ts`).
      dispatch({
        type: "confirmed",
        grantId,
        client_key,
        note: answer?.note,
        seq: (answer as { seq?: unknown } | null | undefined)?.seq,
      });
      // An answer with no change number inserts nothing: the read brings the note.
      reread();
    } catch (err) {
      if (!stillCurrent(e)) return;
      const failure = noteFailure(statusOf(err), codeOf(err), side);
      if (failure === "unconfirmed") {
        // It may have been saved (`504 peer_lost`, a timeout, `429 busy`, no answer). The row
        // stays with its key, so Retry is the same note; the thread is read now, and a note
        // that arrives by that read settles the row (`settleByRead`).
        dispatch({ type: "unconfirmed", grantId, client_key });
        return reread();
      }
      // The owner hid this note: it was saved once and is gone for this side. Not sent again.
      if (failure === "missing") return dispatch({ type: "missing", grantId, client_key });
      // A definite answer: nothing was saved. The draft stays where it is.
      dispatch({
        type: "refused",
        grantId,
        client_key,
        text:
          failure === "rate"
            ? NOTE_RATE
            : failure === "rejected"
              ? (err instanceof ApiError && err.message) || "That note was not saved."
              : "",
      });
      if (failure === "ended") dispatch({ type: "failed", grantId, status: 404 });
      if (failure === "offline") dispatch({ type: "failed", grantId, status: 503 });
    }
  };

  /** Edit, delete or hide ONE note: the server answers with the note as it is now. */
  const change = async (what: "edit" | "delete" | "hide", note: Note, text?: string) => {
    if (busy) return;
    const e = now_.current.epoch;
    setBusy(note.note_id);
    setError(null);
    try {
      await (what === "hide"
          ? api.hideFederationNote(grantId, note.note_id)
          : what === "delete"
            ? side === "owner"
              ? api.deleteFederationNote(grantId, note.note_id)
              : api.deleteFederationRemoteNote(peerId ?? "", grantId, note.note_id)
            : side === "owner"
              ? api.editFederationNote(grantId, note.note_id, text ?? "")
              : api.editFederationRemoteNote(peerId ?? "", grantId, note.note_id, text ?? ""));
      if (!stillCurrent(e)) return;
      // The answer carries no change number, so it cannot be ordered against what the thread
      // has read since (a newer edit, the owner's hide): its note is NOT put into the thread.
      // The thread is read again, and the change arrives in order (THE ORDER RULE).
      reread();
      if (mounted()) setEditing((x) => (x?.id === note.note_id ? null : x));
    } catch (err) {
      if (!stillCurrent(e)) return;
      const failure = noteFailure(statusOf(err), codeOf(err), side);
      if (failure === "ended") dispatch({ type: "failed", grantId, status: 404 });
      else if (failure === "missing") {
        // The note is gone for this side (hidden by the owner): it leaves the thread here too.
        dispatch({ type: "missing", grantId, noteId: note.note_id });
        if (mounted()) setEditing((x) => (x?.id === note.note_id ? null : x));
      } else if (failure === "offline") dispatch({ type: "failed", grantId, status: 503 });
      else if (failure === "unconfirmed") {
        // Neither "saved" nor "not saved": the thread is read again and shows what is there.
        if (mounted()) setError(NOTE_CHANGE_UNCONFIRMED);
        reread();
      } else if (mounted()) setError((err instanceof ApiError && err.message) || "That change was not saved.");
    } finally {
      if (mounted()) setBusy(null);
    }
  };

  const titleId = `notes-title-${grantId}`;
  const shown = thread.status === "live" || thread.status === "ended" ? thread.notes : [];
  const over = [...thread.draft].length > 2000;
  const panel = (
    <aside
      ref={panelRef}
      tabIndex={-1}
      className={styles.drawer}
      role={sheet ? "dialog" : "complementary"}
      aria-modal={sheet ? "true" : undefined}
      aria-labelledby={titleId}
      data-testid="notes-drawer"
      data-grant={grantId}
      data-modal-inside=""
      // Opened from a menu: the menu's focus restore leaves focus here rather than on its trigger.
      data-focus-claim=""
      style={sheet || !place ? undefined : { top: place.top, left: place.left, height: place.height }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className={styles.notesHead}>
        <h2 id={titleId} className={styles.notesTitle} data-testid="notes-title">
          Notes with {other}
        </h2>
        <button ref={closeRef} type="button" className={styles.notesClose} aria-label="Close notes" onClick={close}>
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      {/* Always visible, whatever the thread holds. */}
      <p className={styles.notesLine} data-testid="notes-line">
        {notesLine(other)}
      </p>
      <div className={styles.notesBody} data-testid="notes-body">
        {thread.status === "loading" && (
          <p className={styles.notesEmpty} role="status">
            Loading notes…
          </p>
        )}
        {thread.status === "offline" && (
          <p className={styles.notesEmpty} role="status" data-testid="notes-offline">
            {notesOffline(other)}
          </p>
        )}
        {thread.status === "gone" && (
          <p className={styles.notesEmpty} role="status" data-testid="notes-none">
            Nothing is shown.
          </p>
        )}
        {thread.status === "live" && shown.length === 0 && thread.pending.length === 0 && (
          <p className={styles.notesEmpty} data-testid="notes-empty">
            <b>No notes yet.</b>
            Write to {operatorOf(other)} about this session. Notes stay between the two of you.
          </p>
        )}
        {shown.map((note) => {
          const marks = noteMarks(note, other);
          const actions = noteActions(note, side, thread.status === "live");
          const edit = editing?.id === note.note_id ? editing : null;
          return (
            <article
              key={note.note_id}
              className={`${styles.noteRow} ${note.deleted ? styles.gone : ""} ${note.hidden ? styles.hidden : ""}`}
              data-testid="note"
              data-mine={note.mine ? "1" : "0"}
            >
              <p className={styles.noteMeta}>
                {marks.you ? <span className={styles.you}>You</span> : <PeerBadge name={other} />}
                {/* A peer's time, through the one safe formatter: never a throw, never a crash. */}
                <span data-testid="note-time">{shortTime(note.created_at)}</span>
                {marks.marks.map((m) => (
                  <span key={m} className={styles.mark} data-testid="note-mark">
                    {m}
                  </span>
                ))}
              </p>
              {edit ? (
                <>
                  <textarea
                    className={styles.noteEdit}
                    aria-label="Edit your note"
                    value={edit.text}
                    autoFocus
                    disabled={busy === note.note_id}
                    data-testid="note-edit-box"
                    onChange={(e) => setEditing({ id: note.note_id, text: e.target.value })}
                  />
                  <div className={styles.noteActions}>
                    <button
                      type="button"
                      className={`${btn.primary} ${btn.compact}`}
                      disabled={busy !== null || !noteFits(edit.text) || edit.text === note.text}
                      data-testid="note-save"
                      onClick={() => void change("edit", note, edit.text)}
                    >
                      Save
                    </button>
                    <button type="button" className={`${btn.ghost} ${btn.compact}`} onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </div>
                </>
              ) : (
                // A TEXT NODE. Whatever the other operator typed is characters here.
                <p className={styles.noteText} data-testid="note-text">
                  {note.deleted ? NOTE_DELETED : note.text}
                </p>
              )}
              {!edit && actions.length > 0 && (
                <div className={styles.noteActions}>
                  {actions.includes("edit") && (
                    <button
                      type="button"
                      className={`${btn.ghost} ${btn.compact}`}
                      disabled={busy !== null}
                      data-testid="note-edit"
                      onClick={() => setEditing({ id: note.note_id, text: note.text })}
                    >
                      Edit
                    </button>
                  )}
                  {actions.includes("delete") && (
                    <button
                      type="button"
                      className={`${btn.danger} ${btn.compact}`}
                      disabled={busy !== null}
                      data-testid="note-delete"
                      onClick={() => void change("delete", note)}
                    >
                      Delete
                    </button>
                  )}
                  {actions.includes("hide") && (
                    <button
                      type="button"
                      className={`${btn.ghost} ${btn.compact}`}
                      disabled={busy !== null}
                      aria-label={`Hide this note from ${other}`}
                      data-testid="note-hide"
                      onClick={() => void change("hide", note)}
                    >
                      Hide
                    </button>
                  )}
                </div>
              )}
            </article>
          );
        })}
        {thread.status !== "gone" &&
          thread.status !== "offline" &&
          thread.pending.map((p) => (
            <article key={p.client_key} className={styles.noteRow} data-testid="note-pending" data-state={p.state}>
              <p className={styles.noteMeta}>
                <span className={styles.you}>You</span>
                <span className={styles.noteState}>
                  <Led tone="idle" />
                  {p.state === "sending" ? "sending…" : p.state === "saved" ? "saved" : "not confirmed"}
                </span>
              </p>
              <p className={styles.noteText} data-testid="note-text">
                {p.text}
              </p>
              {p.state === "unconfirmed" && (
                <>
                  <p className={styles.noteHint}>{NOTE_UNCONFIRMED}</p>
                  <div className={styles.noteActions}>
                    <button
                      type="button"
                      className={`${btn.ghost} ${btn.compact}`}
                      disabled={sending || !writable}
                      data-testid="note-retry"
                      onClick={() => void send(p)}
                    >
                      Retry
                    </button>
                  </div>
                </>
              )}
            </article>
          ))}
      </div>
      {error && (
        <p className={styles.notesError} role="alert" data-testid="notes-error">
          {error}
        </p>
      )}
      {thread.refusal && (
        // A CONFIRMED failure: nothing was saved. Not the same as "not confirmed".
        <p className={styles.notesEnded} role="alert" data-testid="notes-refusal">
          {thread.refusal}
        </p>
      )}
      {(thread.status === "gone" || thread.status === "ended") && (
        <p className={styles.notesEnded} role="status" data-testid="notes-ended">
          {thread.status === "gone" ? NOTES_ENDED_PEER : NOTES_ENDED_OWNER}
        </p>
      )}
      {(writable || thread.draft) && (
        <form
          className={styles.notesBottom}
          aria-label={`Write a note to ${operatorOf(other)}`}
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            ref={boxRef}
            className={styles.notesBox}
            rows={1}
            value={thread.draft}
            placeholder="Write a note…"
            aria-label="Write a note…"
            // Off, never emptied: what was typed stays until this drawer closes.
            disabled={!writable}
            autoComplete="off"
            data-testid="notes-box"
            onChange={(e) => dispatch({ type: "draft", grantId, text: e.target.value })}
          />
          <p className={`${styles.notesCount} ${over ? styles.over : ""}`} data-testid="notes-count">
            {noteCounter(thread.draft)}
          </p>
          <button
            type="submit"
            className={`${btn.primary} ${styles.notesGo}`}
            disabled={!writable || sending || !noteFits(thread.draft)}
            data-testid="notes-send"
          >
            {sending ? "Sending…" : "Send note"}
          </button>
        </form>
      )}
    </aside>
  );

  return createPortal(
    sheet ? (
      <div className={styles.sheetScrim} onMouseDown={close} data-testid="notes-sheet">
        {panel}
      </div>
    ) : (
      panel
    ),
    document.body,
  );
}
