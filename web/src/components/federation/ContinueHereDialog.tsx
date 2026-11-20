import { X } from "lucide-react";
import { useEffect, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { useConfig } from "../../app/config";
import { eligibleFor, engineLabel, resolveDefault, useEngineRoster } from "../../app/engineRoster";
import { ApiError, api } from "../../lib/api";
import {
  CONTINUE_BUILDING,
  CONTINUE_BUILDING_NOTE,
  CONTINUE_LEAD,
  CONTINUE_PREVIEW_HEAD,
  CONTINUE_PREVIEW_NOTE,
  CONTINUE_WARNING_WORD,
  continueExplains,
  continueInit,
  continueReduce,
  continueWarning,
  recordLabel,
  seedParts,
  startedFrom,
  type ContinueMode,
} from "../../lib/federationTogether";
import { FolderPickerModal } from "../FolderPickerModal";
import dlg from "../HudDialog.module.css";
import { Led } from "../hud/Meters";
import { useFocusContainment } from "../pulse/useModalDrawer";
import btn from "../ui/actionButton.module.css";
import { PeerBadge } from "./PeerBadge";
import share from "./SharePeer.module.css";
import styles from "./Together.module.css";
import { useAnswerFence } from "./useAnswerFence";

const statusOf = (e: unknown) => (e instanceof ApiError ? e.status : 0);
const codeOf = (e: unknown) =>
  e instanceof ApiError ? (e.record as { code?: unknown } | null | undefined)?.code : undefined;
const baseName = (path: string) => path.replace(/\/+$/, "").split("/").pop() || path;

/** The warning that stands before every control of this dialog, and over the preview. */
function Warning({ owner }: { owner: string }) {
  return (
    <div className={styles.warning} role="note" data-testid="continue-warning">
      <p className={styles.warningWord}>
        <Led tone="att" />
        {CONTINUE_WARNING_WORD}
      </p>
      <p className={styles.warningText}>{continueWarning(owner)}</p>
    </div>
  );
}

/** **Continue here…** (#2062): start this instance's OWN session from what it may read of a
 *  session a peer shares.
 *
 *  **The warning comes first.** After the opening sentence and before any control, on the
 *  attention edge: the text comes from another instance, and the agent is told to treat it as
 *  information, not instructions. It is on screen when the phone sheet opens.
 *
 *  **Preparing creates nothing.** The stages follow the server's prepare → preview → create
 *  (`continueReduce`). Quick copy and Summary both end in the PREVIEW: the seed as plain text,
 *  with the record delimited and labelled. Only *Start session* there calls create, and the
 *  server honours it only after its own last re-read of the source. *Back* and *Cancel* create
 *  nothing. A share that ends while preparing or at create says *Nothing was created.* A create
 *  that got no usable answer (the network, a 5xx, anything unlisted) says neither: the session
 *  may exist, the handle is spent, and the dialog ends on *Check your sessions* with Close only.
 *
 *  **Not editable.** The preview is what the server prepared; this dialog sends back a handle
 *  and a folder, never text. The seed is shown as text nodes only. */
export function ContinueHereDialog({
  peerId,
  grantId,
  owner,
  title,
  onClose,
  returnFocusTo,
}: {
  peerId: string;
  grantId: string;
  owner: string;
  title: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const navigate = useNavigate();
  const config = useConfig();
  const roster = useEngineRoster();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [state, dispatch] = useReducer(continueReduce, continueInit);
  const [mode, setMode] = useState<ContinueMode>("quick");
  const [pickedEngine, setPickedEngine] = useState<string | null>(null);
  const [pickedCwd, setPickedCwd] = useState<string | null>(null);
  const [changing, setChanging] = useState(false);
  const [folderOpen, setFolderOpen] = useState<HTMLElement | null>(null);
  const flight = useRef<AbortController | null>(null);
  // An answer applies only while this dialog is open for the share it was asked for: a fence
  // armed in an effect body, so StrictMode's replay arms it again (`useAnswerFence`, #2061). A
  // prepare in flight is aborted when the dialog goes.
  const ticket = useAnswerFence(`${peerId}/${grantId}`);
  useEffect(() => () => flight.current?.abort(), []);

  // The normal new-session choices: the operator's default agent that can take a seed, and the
  // operator's default folder. Changed through the existing controls.
  const targets = roster.engines.filter((e) => eligibleFor(e, "handoff"));
  const fallback = resolveDefault(roster.engines, config?.agent_defaults?.default_engine ?? null, "handoff").engine;
  const engine = pickedEngine && targets.some((e) => e.id === pickedEngine) ? pickedEngine : fallback;
  const cwd = pickedCwd ?? (config?.default_project || config?.project_roots?.[0] || "");

  const view = state.view;
  const busy = view.stage === "creating";
  useEffect(() => {
    closeRef.current?.focus();
  }, []);
  useFocusContainment({ active: !folderOpen, panelRef: dialogRef });
  useEffect(() => () => returnFocusTo?.focus?.(), [returnFocusTo]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || busy || folderOpen) return;
      e.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy, folderOpen]);

  const prepare = () => {
    if (view.stage !== "choose" || !engine || !cwd) return;
    const token = state.token + 1;
    const ctl = new AbortController();
    flight.current = ctl;
    const current = ticket();
    dispatch({ type: "prepare", mode });
    api.prepareFederationContinue(peerId, grantId, { target_engine: engine, mode }, ctl.signal).then(
      (answer) => {
        if (current() && !ctl.signal.aborted) dispatch({ type: "prepared", token, answer, mode });
      },
      (e: unknown) => {
        if (!current() || ctl.signal.aborted) return;
        dispatch({
          type: "failed",
          token,
          status: statusOf(e),
          code: codeOf(e),
          message: e instanceof ApiError ? e.message : "",
          owner,
        });
      },
    );
  };
  const back = () => {
    flight.current?.abort();
    dispatch({ type: "back" });
  };
  /** THE one place a session is created: Start session, in the preview. */
  const create = async () => {
    if (view.stage !== "preview" || !cwd) return;
    const token = state.token + 1;
    const { handle } = view.prepared;
    const current = ticket();
    dispatch({ type: "create" });
    try {
      const made = await api.createFederationContinue(peerId, grantId, handle, cwd);
      // Remembered in this tab only: the new session's strip. The server keeps no link back.
      startedFrom.set(made.id, owner);
      if (!current()) return;
      navigate(`/s/${encodeURIComponent(made.engine)}/${encodeURIComponent(made.native)}`, {
        state: { fresh: { cwd: made.cwd, bypass: false } },
      });
      onClose();
    } catch (e) {
      // The real outcome, shown once: a failed create is never sent again from here. One that
      // got no usable answer may have created the session: the dialog says so and offers no
      // second create (`continueReduce`, `unconfirmed`).
      if (!current()) return;
      dispatch({
        type: "failed",
        token,
        status: statusOf(e),
        code: codeOf(e),
        message: e instanceof ApiError ? e.message : "",
        owner,
      });
    }
  };

  const titleId = "continue-here-title";
  const prepared = view.stage === "preview" || view.stage === "creating" ? view.prepared : null;
  const parts = prepared ? seedParts(prepared.preview) : null;
  const dismissable = !busy;

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={dismissable ? onClose : undefined}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={view.stage === "preparing" || busy}
        className={`${dlg.dialog} ${dlg.pinned} ${styles.dialog}`}
        // Opened from a menu: the menu's focus restore leaves focus here rather than on its trigger.
        data-focus-claim=""
        data-testid="continue-here"
        data-stage={view.stage}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className={dlg.head}>
          <h2 id={titleId} className={dlg.tag}>
            Continue here
          </h2>
          <button
            ref={closeRef}
            type="button"
            className={`${dlg.close} ${styles.close}`}
            aria-label="Close"
            disabled={!dismissable}
            onClick={onClose}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <p className={`${dlg.from} ${share.from}`}>
          <span className={dlg.fromLabel}>SESSION</span>
          <span className={dlg.fromTitle}>{title}</span>
          <PeerBadge name={owner} />
        </p>

        <div className={`${dlg.sheetBody} ${share.body}`} data-testid="continue-body">
          {view.stage === "choose" && (
            <>
              <p className={styles.lead} data-testid="continue-lead">
                {CONTINUE_LEAD}
              </p>
              {/* Before any control. */}
              <Warning owner={owner} />
              <p className={`${styles.help} ${styles.helpFirst}`}>{continueExplains(owner)}</p>
              <span className={`${dlg.label} ${share.label}`}>New session</span>
              <div className={styles.summaryRow} data-testid="continue-target">
                <span>
                  <b>Project</b> {cwd ? baseName(cwd) : "choose a folder"} · <b>Agent</b>{" "}
                  {engine ? engineLabel(engine) : "none available"}
                </span>
                <button
                  type="button"
                  className={`${btn.ghost} ${btn.compact}`}
                  aria-expanded={changing}
                  data-testid="continue-change"
                  onClick={() => setChanging((v) => !v)}
                >
                  Change
                </button>
              </div>
              {changing && (
                <div className={styles.change}>
                  <div className={share.tiles} role="radiogroup" aria-label="Agent">
                    {targets.map((e) => (
                      <button
                        key={e.id}
                        type="button"
                        role="radio"
                        aria-checked={engine === e.id}
                        className={share.tile}
                        data-engine={e.id}
                        onClick={() => setPickedEngine(e.id)}
                      >
                        <span className={share.tileBody}>
                          <span className={share.tileName}>{engineLabel(e.id)}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    className={btn.ghost}
                    data-testid="continue-folder"
                    onClick={(e) => setFolderOpen(e.currentTarget)}
                  >
                    {cwd ? `Folder: ${cwd}` : "Choose a folder…"}
                  </button>
                </div>
              )}
              <span className={`${dlg.label} ${share.label}`} id="continue-mode">
                Copy as
              </span>
              <div className={`${share.tiles} ${styles.tilesTwo}`} role="radiogroup" aria-labelledby="continue-mode">
                <button
                  type="button"
                  role="radio"
                  aria-checked={mode === "quick"}
                  className={share.tile}
                  data-mode="quick"
                  onClick={() => setMode("quick")}
                >
                  <span className={share.tileBody}>
                    <span className={share.tileName}>Quick copy</span>
                    <span className={share.tileDesc}>The turns you can read, as they are.</span>
                  </span>
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={mode === "ai"}
                  className={share.tile}
                  data-mode="ai"
                  onClick={() => setMode("ai")}
                >
                  <span className={share.tileBody}>
                    <span className={share.tileName}>Summary</span>
                    <span className={share.tileDesc}>
                      A shorter brief of the conversation. Uses your AI endpoint.
                    </span>
                  </span>
                </button>
              </div>
              {view.error && (
                <p className={dlg.error} role="alert" data-testid="continue-error">
                  {view.error}
                </p>
              )}
            </>
          )}
          {view.stage === "preparing" && (
            <div role="status" data-testid="continue-preparing">
              <p className={styles.lead}>{view.mode === "ai" ? CONTINUE_BUILDING : "Preparing the copy…"}</p>
              {/* The Acquiring scan bar: work in progress, never a fake percentage. */}
              <div className={`nj-acquire ${styles.progress}`} aria-hidden="true" />
              <p className={`${styles.help} ${styles.helpFirst}`}>{CONTINUE_BUILDING_NOTE}</p>
            </div>
          )}
          {prepared && parts && (
            <>
              <p className={styles.lead}>{CONTINUE_PREVIEW_HEAD}</p>
              <Warning owner={owner} />
              {prepared.notice && (
                <p className={`${styles.help} ${styles.helpFirst}`} data-testid="continue-notice">
                  {prepared.notice}
                </p>
              )}
              <span className={`${dlg.label} ${share.label}`}>First message of the new session</span>
              {/* Text nodes only. The record is delimited and labelled; it is never markup. */}
              {parts.before && (
                <p className={styles.seedLine} data-testid="continue-seed-before">
                  {parts.before}
                </p>
              )}
              <figure className={styles.seedRecord} data-testid="continue-record">
                <figcaption>{recordLabel(owner)}</figcaption>
                <pre tabIndex={0}>{parts.record}</pre>
              </figure>
              {parts.after && <p className={styles.seedLine}>{parts.after}</p>}
              <p className={`${styles.help} ${styles.helpFirst}`}>{CONTINUE_PREVIEW_NOTE}</p>
            </>
          )}
          {(view.stage === "ended" || view.stage === "offline" || view.stage === "unconfirmed") && (
            <div
              className={styles.unavailable}
              role="alert"
              data-testid={view.stage === "unconfirmed" ? "continue-unconfirmed" : "continue-unavailable"}
            >
              <p className={styles.unavailableWord}>
                <Led tone={view.stage === "unconfirmed" ? "att" : "idle"} />
                {view.stage === "ended" ? "Unavailable" : view.stage === "offline" ? "Offline" : "Not confirmed"}
              </p>
              <p className={styles.warningText}>{view.text}</p>
            </div>
          )}
        </div>

        <div className={`${dlg.actions} ${share.actions}`}>
          {view.stage === "choose" && (
            <>
              <button type="button" className={dlg.cancel} onClick={onClose}>
                Cancel
              </button>
              <button
                type="button"
                className={dlg.go}
                disabled={!engine || !cwd}
                data-testid="continue-prepare"
                onClick={prepare}
              >
                {mode === "ai" ? "Build summary" : "Preview"}
              </button>
            </>
          )}
          {view.stage === "preparing" && (
            <button type="button" className={dlg.cancel} data-testid="continue-cancel" onClick={back}>
              Cancel
            </button>
          )}
          {prepared && (
            <>
              <button type="button" className={dlg.cancel} disabled={busy} data-testid="continue-back" onClick={back}>
                Back
              </button>
              <button
                type="button"
                className={dlg.go}
                disabled={busy || !cwd}
                data-testid="continue-start"
                onClick={() => void create()}
              >
                {busy ? "Starting…" : "Start session"}
              </button>
            </>
          )}
          {(view.stage === "ended" || view.stage === "offline" || view.stage === "unconfirmed") && (
            <button type="button" className={dlg.cancel} data-testid="continue-close" onClick={onClose}>
              Close
            </button>
          )}
        </div>
      </div>
      {folderOpen && (
        <FolderPickerModal
          initialPath={cwd || undefined}
          title="Choose the new session's folder"
          onPick={(path) => {
            setPickedCwd(path);
            setFolderOpen(null);
          }}
          onCancel={() => setFolderOpen(null)}
          returnFocusTo={folderOpen}
        />
      )}
    </div>,
    document.body,
  );
}

/** The new local session's strip (#2062): its first message came from another instance. Shown
 *  for a session this tab started with Continue here; it has no link back. */
export function StartedFromStrip({ sessionKey }: { sessionKey: string }) {
  const owner = startedFrom.get(sessionKey);
  if (!owner) return null;
  return (
    <div className={styles.startedFrom} role="note" data-testid="started-from">
      <PeerBadge name={owner} />
      <span>
        Started from <b>{owner}</b>
        {"'s shared session"}
      </span>
    </div>
  );
}
