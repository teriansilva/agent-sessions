import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { ApiError, api } from "../../lib/api";
import { blockMarkup } from "../../lib/blockMarkup";
import type { NeedsYouDetails } from "../../types/api";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import s from "./AskHome.module.css";
import { optionLabel, sessionRoute, TEXT_VERBS } from "./needsYouLabels";

/** ⓘ on a NEEDS YOU row (#1086): the session's last words, its screen, exactly what approving
 *  does — and, for a text decision, the orchestrator's suggested message AS the action, editable.
 *
 *  Read-only until the operator acts, and it NEVER mounts a terminal: the screen comes from the
 *  server's ring read, so opening this cannot attach a viewer and make Approve refuse (#1049).
 *  One action per Approve — a choice sends its digit, a text decision sends the text left in the
 *  box; nothing is ever queued behind another action. Every agent string is rendered as text. */
export function NeedsYouDetailsDialog({
  sessionId,
  onClose,
  onChanged,
}: {
  sessionId: string;
  onClose: () => void;
  /** Something was decided: the list should re-read. */
  onChanged: () => void;
}) {
  const [d, setD] = useState<NeedsYouDetails | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // After a 409 the evidence on screen is KNOWN to be stale (review 5188): it is marked so, and no
  // decision can be taken from it until a fresh read has succeeded.
  const [outdated, setOutdated] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useFocusContainment({ active: true, panelRef: panel });

  const load = useCallback(async () => {
    try {
      const got = await api.needsYouDetails(sessionId);
      setLoadError(null);
      setOutdated(false);
      setD(got);
      setText(got.action?.suggested_text ?? "");
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Couldn’t read this session.");
    }
  }, [sessionId]);

  // The mount read, inlined so no state is set synchronously in the effect; `load` above is the
  // event-driven re-read after a 409. `live` drops an answer that lands after the dialog closed.
  useEffect(() => {
    let live = true;
    api
      .needsYouDetails(sessionId)
      .then((got) => {
        if (!live) return;
        setLoadError(null);
        setD(got);
        setText(got.action?.suggested_text ?? "");
      })
      .catch((e: unknown) => {
        if (live) setLoadError(e instanceof Error ? e.message : "Couldn’t read this session.");
      });
    closeRef.current?.focus();
    return () => {
      live = false;
    };
  }, [sessionId]);

  // While a decision is IN FLIGHT the dialog cannot be dismissed (review 5184): its completion
  // would otherwise land after the operator had moved on — closing whatever dialog they had open
  // by then and dropping its draft. The parent fences too (it only closes THIS session's dialog).
  const busyRef = useRef(false);
  const dismiss = useCallback(() => {
    if (!busyRef.current) onClose();
  }, [onClose]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        dismiss();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dismiss]);

  const act = async (fn: () => Promise<unknown>) => {
    if (busy) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Shown once the fresh read has landed; until then the out-of-date banner speaks.
        setError("The session moved on since you opened this — here is where it is now.");
        setOutdated(true);
        void load(); // show the CURRENT screen, never act on a frozen one
      } else {
        setError(e instanceof Error ? e.message : "That didn’t go through.");
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  // No decision from evidence known to be stale — only after a fresh read.
  const action = outdated ? null : (d?.action ?? null);
  const isText = Boolean(action?.editable && TEXT_VERBS.has(action.verb));
  const isChoose = action?.verb === "choose" && typeof action.option === "number";
  // An escalation at a numbered menu: its options, answered through `/choose` (#1060).
  const escalatedMenu =
    action && !action.can_approve && action.menu && action.menu.options.length
      ? action.menu
      : null;
  // The screen is shown only where it is evidence the last words don't carry (#1169): a numbered
  // menu or permission prompt (what is being approved lives only on screen), or a session with no
  // last words yet. Otherwise it repeats the final message plus the agent's own input box and
  // status chrome — for every engine, so this is decided on the payload, never the engine.
  const showScreen = !d?.last_words || Boolean(d?.menu) || isChoose || Boolean(action?.menu);
  const titleId = "needs-you-details-title";

  return createPortal(
    // `data-modal-inside` (#1294, Hermes on #1296): this dialog can be opened from the Ask
    // drawer, which is a modal on a phone, and it is portalled to <body> — so without the marker a
    // press on this backdrop read as "outside the drawer" and one tap closed both. A press here
    // belongs to THIS dialog; `useModalDrawer` skips surfaces that declare themselves inside.
    <div className={dlg.backdrop} onMouseDown={dismiss} data-modal-inside="">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`${dlg.dialog} ${s.details}`}
        onMouseDown={(e) => e.stopPropagation()}
        data-testid="needs-you-dialog"
      >
        <div className={dlg.head}>
          <span id={titleId} className={dlg.tag}>
            {d?.title || "Session details"}
          </span>
          <button
            ref={closeRef}
            type="button"
            className={dlg.close}
            onClick={dismiss}
            disabled={busy}
            aria-label="Close details"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        {loadError && !d ? (
          <p className={dlg.error} role="alert">
            {loadError}
          </p>
        ) : !d ? (
          <p className={dlg.muted}>Reading…</p>
        ) : (
          <>
            <p className={dlg.from}>
              <span className={dlg.fromLabel}>
                {d.engine}
                {d.project.name ? ` · ${d.project.name}` : ""} //
              </span>
            </p>
            {d.last_words ? (
              <>
                <div className={s.lab}>Session’s last words</div>
                <div className={`${s.quote} ${s.md}`} data-testid="needs-you-last-words">
                  {blockMarkup(d.last_words)}
                </div>
              </>
            ) : null}
            {outdated ? (
              <p className={dlg.error} role="alert" data-testid="needs-you-outdated">
                {loadError
                  ? "The session moved on, and its current screen couldn’t be read — what is below is out of date."
                  : "The session moved on — reading where it is now…"}{" "}
                {loadError ? (
                  <button type="button" className={s.btn} onClick={() => void load()}>
                    Retry
                  </button>
                ) : null}
              </p>
            ) : null}
            {showScreen ? (
              <>
                <div className={s.lab}>
                  {outdated
                    ? "On screen before (out of date)"
                    : "On screen now · read without attaching"}
                </div>
                <pre className={s.screen} data-testid="needs-you-screen">
                  {d.screen || "(nothing on screen)"}
                </pre>
              </>
            ) : null}

            {isChoose ? (
              <>
                <div className={s.lab}>What approving does</div>
                <div className={s.approve}>
                  Selects <b>option {action!.option}</b>
                  {optionLabel(action!.menu ?? d.menu, action!.option)
                    ? ` — “${optionLabel(action!.menu ?? d.menu, action!.option)}”`
                    : ""}
                  .
                  <div className={s.bytes}>
                    Sends <code>{action!.option}</code> <code>⏎</code> · only if the screen still
                    shows this question
                  </div>
                </div>
              </>
            ) : null}

            {isText ? (
              <>
                <label className={s.lab} htmlFor="needs-you-text">
                  Orchestrator suggests sending · you can edit — this is what Approve sends
                </label>
                <textarea
                  id="needs-you-text"
                  className={s.textarea}
                  value={text}
                  readOnly={busy}
                  maxLength={4000}
                  onChange={(e) => setText(e.target.value)}
                  data-testid="needs-you-text"
                />
                <div className={s.bytes}>
                  Sent as a pasted message + <code>⏎</code> · only if the prompt is still idle
                </div>
              </>
            ) : null}

            {escalatedMenu ? (
              <>
                <div className={s.lab}>{escalatedMenu.question || "Choose an answer"}</div>
                <div className={s.options}>
                  {escalatedMenu.options.map((o) => (
                    <button
                      key={o.n}
                      type="button"
                      className={s.btn}
                      disabled={busy}
                      onClick={() =>
                        void act(() => api.chooseAction(action!.id, o.n, o.label))
                      }
                    >
                      {o.n}. {o.label}
                    </button>
                  ))}
                </div>
              </>
            ) : null}

            {!action && !outdated ? (
              <p className={dlg.help}>
                Nothing here can be approved from Ask — {d.reason || "open the session to answer."}
              </p>
            ) : null}

            {error && !outdated ? (
              <p className={dlg.error} role="alert" data-testid="needs-you-error">
                {error}
              </p>
            ) : null}

            <div className={s.foot}>
              <Link className={s.btn} to={sessionRoute(d.id)} onClick={dismiss}>
                Open session
              </Link>
              <button
                type="button"
                className={`${s.btn} ${s.quiet}`}
                disabled={busy || outdated}
                onClick={() => void act(() => api.needsYouDismiss(d.id, action?.id))}
                data-testid="needs-you-dismiss"
              >
                Dismiss
              </button>
              <span className={s.sp} />
              {isChoose && action!.can_approve ? (
                <button
                  type="button"
                  className={`${s.btn} ${s.primary}`}
                  disabled={busy}
                  onClick={() => void act(() => api.approveAction(action!.id))}
                  data-testid="needs-you-dialog-approve"
                >
                  Approve · option {action!.option}
                </button>
              ) : null}
              {isText && action!.can_approve ? (
                <button
                  type="button"
                  className={`${s.btn} ${s.primary}`}
                  disabled={busy || !text.trim()}
                  onClick={() =>
                    void act(() =>
                      api.approveAction(
                        action!.id,
                        text === (action!.suggested_text ?? "") ? undefined : text,
                      ),
                    )
                  }
                  data-testid="needs-you-dialog-approve"
                >
                  Approve · send
                </button>
              ) : null}
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
