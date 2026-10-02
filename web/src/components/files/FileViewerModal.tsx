import {
  lazy,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { UNSAFE_DataRouterContext, useBlocker } from "react-router-dom";
import { Pencil, X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { parseDiff } from "../../lib/diffParse";
import type {
  FileContent,
  FileWriteRefusal,
  FileWriteResult,
  GitDiff,
} from "../../types/api";
import type { CodeEditorHandle } from "./editor/CodeEditor";
import { languageLabel } from "./editor/languages";
import styles from "./filePanel.module.css";

// The editor is its own chunk: a session that never opens a file never downloads CodeMirror.
const CodeEditor = lazy(() => import("./editor/CodeEditor"));

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; at: number; retained: FileWriteResult["retained"] }
  | {
      kind: "conflict";
      message: string;
      /** The on-disk version once it has been LOOKED AT — overwrite is bound to this. */
      disk: { content: string; version: string } | null;
      viewing: "mine" | "disk";
      /** Whether the operator's draft differs from the last saved text. Recorded here because
       *  while ON DISK is shown the editor holds the comparison text, and asking the editor would
       *  describe that instead of the draft. */
      draftDirty: boolean;
      /** A read that failed while comparing. The conflict, the draft and OVERWRITE all survive it —
       *  the error is shown beside them, never instead of them. */
      error: string | null;
    }
  | { kind: "busy"; message: string; holder: FileWriteRefusal["holder"] }
  | { kind: "both"; message: string; paths: string[] }
  | { kind: "error"; message: string; retry: boolean };

function refusal(e: unknown): SaveState {
  if (e instanceof ApiError && e.status === 409 && e.record && typeof e.record === "object") {
    const r = e.record as FileWriteRefusal;
    const message = r.detail || e.message;
    if (r.both?.length) return { kind: "both", message, paths: r.both };
    if (r.reason === "changed")
      return { kind: "conflict", message, disk: null, viewing: "mine", draftDirty: true, error: null };
    if (r.reason === "open_elsewhere") return { kind: "busy", message, holder: r.holder };
    return { kind: "error", message, retry: r.reason === "opened_during_save" };
  }
  return {
    kind: "error",
    message: e instanceof ApiError ? e.message : "Could not save the file.",
    retry: true,
  };
}

function readFailure(e: unknown): string {
  return e instanceof Error && e.message ? e.message : "Could not read the file on disk.";
}

/** A server reason is a predicate ("is larger than 1 MiB …"), so it reads after the file name;
 *  a capability reason is already a sentence ("editing is off: …"). */
function reasonSentence(name: string, reason: string): string {
  return /^(is|has|mixes|uses|cannot)\b/.test(reason)
    ? `${name} ${reason}.`
    : `${reason.charAt(0).toUpperCase()}${reason.slice(1)}.`;
}

const normalize = (text: string) => text.replace(/\r\n?/g, "\n");

/** Server refusals are written as clauses ("the file changed on disk …"); a banner shows them as
 *  sentences. */
function sentence(text: string): string {
  const t = text.trim();
  if (!t) return t;
  const s = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

/** Holds in-app navigation while there are unsaved edits. Rendered only under a DATA router —
 *  `useBlocker` throws anywhere else, and a viewer mounted outside one simply has no guard. */
function NavigationGuard({
  dirty,
  onBlocked,
}: {
  dirty: boolean;
  onBlocked: (actions: { proceed: () => void; reset: () => void } | null) => void;
}) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      (currentLocation.pathname !== nextLocation.pathname ||
        currentLocation.search !== nextLocation.search),
  );
  useEffect(() => {
    if (blocker.state === "blocked") {
      onBlocked({ proceed: () => blocker.proceed(), reset: () => blocker.reset() });
    }
  }, [blocker, onBlocked]);
  return null;
}

/** On a phone the viewer follows the VISUAL viewport, so SAVE and the edited line stay above the
 *  on-screen keyboard instead of behind it. Desktop keeps the stylesheet's inset box. */
function useVisualViewportBox(): { top: number; height: number } | null {
  const measure = () => {
    if (typeof window === "undefined") return null;
    const vv = window.visualViewport;
    if (!vv || !window.matchMedia?.("(pointer: coarse)").matches) return null;
    return { top: vv.offsetTop, height: vv.height };
  };
  const [box, setBox] = useState(measure);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setBox(measure());
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);
  return box;
}

/** File viewer (#783) and editor (#950) — an OVERLAY, never a pane split: opening a file must not
 *  evict, resize, or reflow the live terminal.
 *
 *  Portalled to <body> for two reasons, both load-bearing rather than stylistic. (1) `.terminal-pane`
 *  is `overflow: hidden`, so an in-tree overlay is clipped. (2) The terminal's coarse-pointer
 *  touch-capture layer sits at z-index 6 with `touch-action: none` and swallows touches — an
 *  overlay rendered inside that subtree is simply not tappable on a phone.
 *
 *  Dialog contract: `role="dialog"` + `aria-modal`, focus moved in on open, focus CONTAINED while
 *  open, focus returned to the trigger on close, Esc closes, and body scroll locked so the page
 *  behind cannot move under a touch drag. With unsaved edits every way out — ✕, the scrim, Esc,
 *  in-app navigation, a reload — asks first.
 *
 *  Saving is bound to the version the file was LOADED at. The server refuses a save whose version
 *  moved (usually the agent wrote the file), and the refusal keeps the operator's text in the
 *  editor: ON DISK shows what changed, and OVERWRITE is only offered once it has been looked at,
 *  so a save never replaces bytes the operator was not shown. */
export function FileViewerModal({
  path,
  onClose,
  returnFocusTo,
  modes,
  staged = false,
  onOpenPath,
}: {
  path: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  /** Which modes apply to THIS row. A mode that does not apply is absent, never a dead control:
   *  a deleted file has nothing left to read, an untracked one nothing to compare against. */
  modes?: { diff: boolean; content: boolean; defaultDiff: boolean };
  staged?: boolean;
  /** Open another path in the viewer — how a kept previous version is reached after a save. */
  onOpenPath?: (path: string) => void;
}) {
  const showDiff = modes?.diff ?? false;
  const showContent = modes?.content ?? true;
  const [mode, setMode] = useState<"diff" | "content">(
    modes?.defaultDiff && modes.diff ? "diff" : "content",
  );
  // Tag the response with the request it answers so "loading" is derived, not set synchronously
  // inside the effect.
  const sig = `${path}:${staged ? 1 : 0}`;
  const [diffRes, setDiffRes] = useState<{
    sig: string;
    d?: GitDiff;
    message?: string;
  } | null>(null);

  useEffect(() => {
    if (mode !== "diff") return;
    let live = true;
    const ctl = new AbortController();
    api
      .gitDiff(path, staged, { signal: ctl.signal })
      .then((d) => live && setDiffRes({ sig, d }))
      .catch((e: unknown) => {
        if (!live || (e instanceof DOMException && e.name === "AbortError"))
          return;
        setDiffRes({
          sig,
          message:
            e instanceof ApiError ? e.message : "Could not build the diff.",
        });
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [mode, path, staged, sig]);

  const diff:
    | { kind: "loading" }
    | { kind: "ok"; d: GitDiff }
    | { kind: "error"; message: string } =
    diffRes?.sig !== sig
      ? { kind: "loading" }
      : diffRes.d
        ? { kind: "ok", d: diffRes.d }
        : {
            kind: "error",
            message: diffRes.message ?? "Could not build the diff.",
          };
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "ok"; file: FileContent }
    | { kind: "error"; message: string }
  >({ kind: "loading" });
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLDivElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const editorRef = useRef<CodeEditorHandle>(null);

  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [version, setVersion] = useState<string | null>(null);
  const [save, setSave] = useState<SaveState>({ kind: "idle" });
  const [cursor, setCursor] = useState<{ line: number; col: number } | null>(null);
  const [confirmClose, setConfirmClose] = useState(false);
  const [pendingNav, setPendingNav] = useState<{
    proceed: () => void;
    reset: () => void;
  } | null>(null);
  const dirtyRef = useRef(false);
  const savedText = useRef("");
  /** The operator's text while the editor shows something else (ON DISK). While Mine is shown the
   *  editor IS the draft, so this is only read across that swap. */
  const draftRef = useRef<string | null>(null);
  /** `save` as of the last transition, for handlers that finish after an await. */
  const saveRef = useRef<SaveState>({ kind: "idle" });
  /** Bumped by every conflict action that takes ownership. A read that finishes under an older
   *  value lost the race and changes nothing. */
  const conflictOwner = useRef(0);
  /** The conflict read still out — ON DISK's or RELOAD's — cleared by whatever takes ownership.
   *  State, not a ref, because the render fences on it: ON DISK's completion reads the draft from a
   *  mounted editor, and RELOAD's replaces the editor text, so nothing may be typed into it meanwhile. */
  const [pendingRead, setPendingRead] = useState<"disk" | "reload" | null>(null);
  /** Whether the editor is mounted. While Mine is shown it is the only holder of the draft. */
  const [editorReady, setEditorReady] = useState(false);
  /** A path the viewer was asked to switch to while there were unsaved edits. */
  const [confirmOpen, setConfirmOpen] = useState<string | null>(null);
  const viewportBox = useVisualViewportBox();
  const inDataRouter = useContext(UNSAFE_DataRouterContext) != null;

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  const setSaveState = useCallback((next: SaveState) => {
    saveRef.current = next;
    setSave(next);
  }, []);

  /** Take ownership of the conflict, naming the read this action leaves out, if any. */
  const takeConflict = useCallback((read: "disk" | "reload" | null) => {
    setPendingRead(read);
    return ++conflictOwner.current;
  }, []);

  const attachEditor = useCallback((handle: CodeEditorHandle | null) => {
    editorRef.current = handle;
    setEditorReady(handle !== null);
  }, []);

  /** The operator's draft: the retained copy while ON DISK is shown, the editor while Mine is.
   *  `null` when the editor holding it is not mounted — an unknown draft is refused, never sent
   *  as "". */
  const readDraft = useCallback(
    (s: Extract<SaveState, { kind: "conflict" }>) =>
      s.viewing === "disk" ? draftRef.current : (editorRef.current?.getText() ?? null),
    [],
  );

  // The draft's dirtiness, wherever the draft lives: the editor's flag while it holds the draft,
  // the conflict's own record while ON DISK is on screen.
  const unsaved = save.kind === "conflict" && save.viewing === "disk" ? save.draftDirty : dirty;

  // No synchronous `setState({kind:"loading"})` here: the parent keys this component by path, so
  // opening a different file REMOUNTS it and the initial state is already "loading".
  useEffect(() => {
    let live = true;
    const ctl = new AbortController();
    api
      .filesRead(path, { signal: ctl.signal })
      .then((file) => {
        if (!live) return;
        savedText.current = normalize(file.content ?? "");
        setVersion(file.version ?? null);
        setState({ kind: "ok", file });
      })
      .catch((e: unknown) => {
        if (!live || (e instanceof DOMException && e.name === "AbortError"))
          return;
        setState({
          kind: "error",
          message:
            e instanceof ApiError ? e.message : "Could not read this file.",
        });
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [path]);

  const reallyClose = useCallback(() => {
    onClose();
    // Return focus to whatever opened us — a11y, and it keeps keyboard tree navigation usable.
    if (returnFocusTo && document.contains(returnFocusTo))
      returnFocusTo.focus();
  }, [onClose, returnFocusTo]);

  // Asks the EDITOR, not React state: the render carrying the last keystroke may not have happened
  // yet when Esc or ✕ arrives, and reading stale state here closes the viewer over unsaved text.
  // Except while ON DISK is shown: the editor then holds the comparison text, which can equal the
  // loaded version exactly (the agent put the file back), so the retained draft is what is asked.
  const hasUnsaved = useCallback(() => {
    const s = saveRef.current;
    if (s.kind === "conflict" && s.viewing === "disk") {
      return normalize(draftRef.current ?? "") !== savedText.current;
    }
    return editorRef.current?.isDirty() ?? dirtyRef.current;
  }, []);

  const close = useCallback(() => {
    if (hasUnsaved()) setConfirmClose(true);
    else reallyClose();
  }, [hasUnsaved, reallyClose]);

  /** Open another path from inside the viewer — a kept version, after a save or a refusal. It
   *  replaces this viewer, so it asks about unsaved edits exactly as closing does. */
  const openPath = useCallback(
    (p: string) => {
      if (!onOpenPath) return;
      if (hasUnsaved()) setConfirmOpen(p);
      else onOpenPath(p);
    },
    [hasUnsaved, onOpenPath],
  );

  const asking = confirmClose || pendingNav !== null || confirmOpen !== null;

  const keepEditing = useCallback(() => {
    pendingNav?.reset();
    setPendingNav(null);
    setConfirmClose(false);
    setConfirmOpen(null);
    requestAnimationFrame(() => editorRef.current?.focus());
  }, [pendingNav]);

  const discardAndLeave = useCallback(() => {
    dirtyRef.current = false;
    setConfirmClose(false);
    if (pendingNav) {
      const nav = pendingNav;
      setPendingNav(null);
      nav.proceed();
      return;
    }
    if (confirmOpen !== null) {
      const next = confirmOpen;
      setConfirmOpen(null);
      onOpenPath?.(next);
      return;
    }
    reallyClose();
  }, [confirmOpen, onOpenPath, pendingNav, reallyClose]);

  // Focus in on open.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    if (asking) keepRef.current?.focus();
  }, [asking]);

  // Body scroll lock: without it a touch drag over the scrim scrolls the page behind the overlay.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  // A reload or a closed tab with unsaved edits asks through the browser's own prompt.
  useEffect(() => {
    if (!unsaved) return;
    const onUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [unsaved]);

  // Esc + focus containment. While the unsaved-edits question is up, it owns the keyboard.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (asking) {
          e.stopPropagation();
          keepEditing();
          return;
        }
        // Esc inside the editor's search panel closes the panel, not the viewer.
        if ((e.target as HTMLElement | null)?.closest?.(".cm-panels")) return;
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const root = asking ? confirmRef.current : panelRef.current;
      if (!root) return;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input, select, textarea, [contenteditable="true"], [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !root.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !root.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [asking, close, keepEditing]);

  const doSave = useCallback(
    async (content: string, expect: string) => {
      takeConflict(null);
      setSaveState({ kind: "saving" });
      try {
        const r = await api.filesWrite(path, content, expect);
        savedText.current = normalize(content);
        draftRef.current = null;
        setVersion(r.version);
        editorRef.current?.markClean(content);
        // The saved text is the document from here on — including for an editor that remounts
        // (Diff → Content), which starts from `file.content`. Updating only the version paired the
        // new token with the old text, so the next save silently reverted this one.
        setState((s) =>
          s.kind === "ok"
            ? { kind: "ok", file: { ...s.file, content, size: r.size, version: r.version } }
            : s,
        );
        setSaveState({ kind: "saved", at: Date.now(), retained: r.retained });
      } catch (e) {
        setSaveState(refusal(e));
      }
    },
    [path, setSaveState, takeConflict],
  );

  const saveNow = useCallback(() => {
    const ed = editorRef.current;
    const s = saveRef.current;
    if (!ed || !version || !editing || !ed.isDirty()) return;
    if (s.kind === "saving" || (s.kind === "conflict" && s.viewing === "disk")) return;
    void doSave(ed.getText(), version);
  }, [doSave, editing, version]);

  const readDisk = useCallback(async () => {
    const f = await api.filesRead(path);
    if (f.binary || f.content == null || !f.version) {
      throw new Error("The file on disk can no longer be edited here.");
    }
    return { file: f, content: f.content, version: f.version };
  }, [path]);

  const viewDisk = useCallback(async () => {
    if (saveRef.current.kind !== "conflict") return;
    const owner = takeConflict("disk");
    let d: Awaited<ReturnType<typeof readDisk>>;
    try {
      d = await readDisk();
    } catch (e) {
      if (owner !== conflictOwner.current) return;
      setPendingRead(null);
      const s = saveRef.current;
      if (s.kind === "conflict") setSaveState({ ...s, error: readFailure(e) });
      return;
    }
    // Mine, Reload or Overwrite took over while this read was out: it lost, and changes nothing.
    if (owner !== conflictOwner.current) return;
    setPendingRead(null);
    const s = saveRef.current;
    if (s.kind !== "conflict") return;
    // The draft is captured HERE, as the disk text replaces it — not before the read. The editor
    // stays editable while the read is out, and what was typed in that window is part of it.
    const draft = readDraft(s);
    if (draft === null) {
      setSaveState({
        ...s,
        error: "your edits were not loaded in the editor, so ON DISK was not opened",
      });
      return;
    }
    draftRef.current = draft;
    editorRef.current?.show(d.content);
    setSaveState({
      ...s,
      disk: { content: d.content, version: d.version },
      viewing: "disk",
      draftDirty: normalize(draft) !== savedText.current,
      error: null,
    });
  }, [readDisk, readDraft, setSaveState, takeConflict]);

  const viewMine = useCallback(() => {
    const s = saveRef.current;
    if (s.kind !== "conflict") return;
    // Choosing Mine takes ownership either way, so an ON DISK read still out cannot land after it.
    takeConflict(null);
    // Already showing Mine, the editor IS the draft: putting a copy back would erase newer typing.
    if (s.viewing === "mine") return;
    editorRef.current?.show(draftRef.current ?? "");
    draftRef.current = null;
    setSaveState({ ...s, viewing: "mine", error: null });
  }, [setSaveState, takeConflict]);

  const reloadFromDisk = useCallback(async () => {
    // The completion replaces the editor text, so the editor takes no typing until it lands
    // (`pendingRead`): anything typed in that window would be shown, then silently thrown away.
    const owner = takeConflict("reload");
    let d: Awaited<ReturnType<typeof readDisk>>;
    try {
      d = await readDisk();
    } catch (e) {
      if (owner !== conflictOwner.current) return;
      setPendingRead(null);
      const s = saveRef.current;
      // A failed read replaces nothing: the conflict, the draft and OVERWRITE stay where they are.
      if (s.kind === "conflict") setSaveState({ ...s, error: readFailure(e) });
      else setSaveState({ kind: "error", message: readFailure(e), retry: false });
      return;
    }
    if (owner !== conflictOwner.current) return;
    setPendingRead(null);
    savedText.current = normalize(d.content);
    draftRef.current = null;
    editorRef.current?.reset(d.content);
    setVersion(d.version);
    setState({ kind: "ok", file: d.file });
    setSaveState({ kind: "idle" });
  }, [readDisk, setSaveState, takeConflict]);

  const overwrite = useCallback(() => {
    const s = saveRef.current;
    if (s.kind !== "conflict" || !s.disk) return;
    // One authoritative draft: the live editor while Mine is shown, the retained copy while ON DISK
    // is. A snapshot taken when the conflict arrived would drop everything typed since. A draft
    // nobody holds sends nothing: the button is fenced on it, and a click already queued lands here.
    const text = readDraft(s);
    if (text === null) return;
    if (s.viewing === "disk") editorRef.current?.show(text);
    void doSave(text, s.disk.version);
  }, [doSave, readDraft]);

  const discardEdits = useCallback(() => {
    takeConflict(null);
    draftRef.current = null;
    editorRef.current?.reset(savedText.current);
    setSaveState({ kind: "idle" });
  }, [setSaveState, takeConflict]);

  const startEditing = useCallback(() => {
    setEditing(true);
    requestAnimationFrame(() => editorRef.current?.focus());
  }, []);

  const name = path.split("/").pop() || path;
  const textFile = state.kind === "ok" && !state.file.binary ? state.file : null;
  const lines = textFile ? (textFile.content ?? "").split("\n") : [];
  const editable = Boolean(textFile?.editable && version);
  const readonlyReason = textFile && !editable ? textFile.readonly_reason : null;
  const viewingDisk = save.kind === "conflict" && save.viewing === "disk";
  const editorEditable =
    editing && !viewingDisk && save.kind !== "saving" && pendingRead !== "reload";
  // While Mine is shown the draft exists only in the mounted editor; ON DISK keeps its own copy.
  const draftKnown = save.kind !== "conflict" || save.viewing === "disk" || editorReady;
  // Leaving CONTENT unmounts the editor. Held while a comparison read is out, whose completion reads
  // the draft from that editor, and while ON DISK is shown, because a remount starts from the loaded
  // text rather than the version on disk the banner says is on screen.
  const diffFence = unsaved
    ? "Save or discard your edits first"
    : pendingRead
      ? "Wait for the file on disk to finish loading"
      : viewingDisk
        ? "Go back to Mine first — Diff would drop the version on disk you are comparing"
        : null;
  const showModeBar = (showDiff && showContent) || (mode === "content" && textFile !== null);
  const saving = save.kind === "saving";

  return createPortal(
    <>
      <button
        type="button"
        className={styles.viewerScrim}
        aria-label="Dismiss the file viewer"
        onClick={close}
      />
      <div
        ref={panelRef}
        className={styles.viewer}
        role="dialog"
        aria-modal="true"
        aria-label={`File: ${name}`}
        data-file-viewer=""
        style={
          viewportBox
            ? { top: viewportBox.top, height: viewportBox.height, bottom: "auto" }
            : undefined
        }
      >
        {inDataRouter && <NavigationGuard dirty={unsaved} onBlocked={setPendingNav} />}
        <div className={styles.viewerHead}>
          <div className={styles.viewerTitles}>
            <div className={styles.viewerName}>{name}</div>
            <div className={styles.viewerPath}>{path}</div>
          </div>
          <button
            ref={closeRef}
            type="button"
            className={styles.iconBtn}
            onClick={close}
            aria-label="Close file viewer"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        {showModeBar && (
          <div
            className={`${styles.modeBar} ${styles.modeBarWrap}`}
            role="group"
            aria-label="View mode"
          >
            {showDiff && showContent && (
              <>
                <button
                  type="button"
                  className={`${styles.modeBtn} ${mode === "diff" ? styles.modeBtnOn : ""}`}
                  aria-pressed={mode === "diff"}
                  // The editor lives in CONTENT; see `diffFence` for when leaving it is held.
                  disabled={diffFence !== null}
                  title={diffFence ?? undefined}
                  onClick={() => setMode("diff")}
                >
                  Diff
                </button>
                <button
                  type="button"
                  className={`${styles.modeBtn} ${mode === "content" ? styles.modeBtnOn : ""}`}
                  aria-pressed={mode === "content"}
                  onClick={() => setMode("content")}
                >
                  Content
                </button>
              </>
            )}
            {!(showDiff && showContent) && textFile && (
              // A file opened from the tree has no DIFF/CONTENT pair; the bar still says which
              // state the document is in, so its left side is information rather than a gap.
              <span className={`hud-tag ${styles.viewerStateTag}`} data-viewer-state="">
                {editing ? "Content // Editing" : "Content // Reading"}
              </span>
            )}
            <span className={styles.modeSpacer} />
            {mode === "diff" && (
              <span className="hud-tag">
                {/* Withheld rather than guessed: the server nulls these when the diff was cut
                    off, because a count taken from a prefix is not a total. */}
                {diff.kind === "ok" && diff.d.added !== null && diff.d.removed !== null
                  ? `+${diff.d.added} −${diff.d.removed}`
                  : diff.kind === "ok" && diff.d.truncated
                    ? "COUNTS UNAVAILABLE"
                    : ""}
              </span>
            )}
            {mode === "content" &&
              textFile &&
              (!editing ? (
                <button
                  type="button"
                  className={styles.ctrlBtn}
                  disabled={!editable}
                  title={
                    editable ? "Edit this file" : `Read-only — ${readonlyReason ?? "not editable"}`
                  }
                  onClick={startEditing}
                  data-edit-toggle=""
                >
                  <Pencil size={12} aria-hidden="true" /> Edit
                </button>
              ) : (
                <>
                  {unsaved && (
                    <span className={styles.unsaved} data-unsaved="">
                      Unsaved
                    </span>
                  )}
                  {unsaved ? (
                    <button
                      type="button"
                      className={styles.ctrlBtn}
                      onClick={discardEdits}
                      disabled={saving || viewingDisk}
                      data-discard-edits=""
                    >
                      Discard edits
                    </button>
                  ) : (
                    <button
                      type="button"
                      className={styles.ctrlBtn}
                      onClick={() => setEditing(false)}
                      data-edit-done=""
                    >
                      Done
                    </button>
                  )}
                  <button
                    type="button"
                    className={styles.cta}
                    onClick={saveNow}
                    disabled={!unsaved || saving || viewingDisk}
                    title="Save (Ctrl/Cmd+S)"
                    data-save=""
                  >
                    {saving ? "Saving…" : "Save"}
                  </button>
                </>
              ))}
          </div>
        )}

        {mode === "content" && readonlyReason && (
          <div className={styles.banner} data-readonly-reason="">
            <span className={styles.bannerTag}>Read-only</span>
            <p className={styles.bannerText}>{reasonSentence(name, readonlyReason)}</p>
          </div>
        )}
        {mode === "content" && save.kind === "conflict" && (
          <div
            className={`${styles.banner} ${styles.bannerWarn}`}
            role="alert"
            data-save-conflict=""
          >
            <span className={styles.bannerTag}>Changed on disk</span>
            <p className={styles.bannerText}>{sentence(save.message)}</p>
            <div className={styles.bannerRow}>
              <div className={styles.seg} role="group" aria-label="Compare versions">
                <button
                  type="button"
                  className={`${styles.segBtn} ${save.viewing === "mine" ? styles.segBtnOn : ""}`}
                  aria-pressed={save.viewing === "mine"}
                  onClick={viewMine}
                  data-conflict-view="mine"
                >
                  Mine
                </button>
                <button
                  type="button"
                  className={`${styles.segBtn} ${save.viewing === "disk" ? styles.segBtnOn : ""}`}
                  aria-pressed={save.viewing === "disk"}
                  onClick={() => void viewDisk()}
                  data-conflict-view="disk"
                >
                  On disk
                </button>
              </div>
              <span className={styles.modeSpacer} />
              <button
                type="button"
                className={styles.ctrlBtn}
                onClick={() => void reloadFromDisk()}
                data-reload-disk=""
              >
                Reload from disk
              </button>
              <button
                type="button"
                className={`${styles.ctrlBtn} ${save.disk && draftKnown ? styles.ctrlBad : ""}`}
                disabled={!save.disk || !draftKnown}
                title={
                  !save.disk
                    ? "Look at ON DISK first — a save never replaces bytes you were not shown"
                    : !draftKnown
                      ? "Your edits are still loading into the editor"
                      : "Replace the version on disk with yours"
                }
                onClick={overwrite}
                data-overwrite=""
              >
                Overwrite with mine
              </button>
            </div>
            {!save.disk && (
              <p className={styles.bannerHint}>
                Overwrite unlocks once you have looked at the version on disk.
              </p>
            )}
            {save.disk && !draftKnown && (
              <p className={styles.bannerHint}>
                Overwrite unlocks once your edits are back in the editor.
              </p>
            )}
            {pendingRead && (
              <p className={styles.bannerHint} role="status" data-conflict-pending="">
                {pendingRead === "reload"
                  ? "Reading the file on disk — the editor takes no typing until it has loaded."
                  : "Reading the version on disk — Diff waits until it has loaded."}
              </p>
            )}
            {save.error && (
              <p className={styles.bannerHint} role="status" data-conflict-error="">
                {sentence(save.error)} Nothing changed — your edits are still here.
              </p>
            )}
          </div>
        )}
        {mode === "content" && save.kind === "busy" && (
          <div
            className={`${styles.banner} ${styles.bannerWarn}`}
            role="alert"
            data-save-busy=""
          >
            <span className={styles.bannerTag}>Open in another process</span>
            <p className={styles.bannerText}>
              {sentence(save.message)}
              {save.holder?.comm ? ` Held by ${save.holder.comm} (pid ${save.holder.pid}).` : ""}
            </p>
            <div className={styles.bannerRow}>
              <button type="button" className={styles.ctrlBtn} onClick={saveNow}>
                Try again
              </button>
            </div>
          </div>
        )}
        {mode === "content" && save.kind === "both" && (
          <div
            className={`${styles.banner} ${styles.bannerWarn}`}
            role="alert"
            data-save-both=""
          >
            <span className={styles.bannerTag}>Both versions kept</span>
            <p className={styles.bannerText}>{sentence(save.message)}</p>
            <div className={styles.bannerRow}>
              {save.paths.map((p) =>
                onOpenPath && p !== path ? (
                  <button
                    key={p}
                    type="button"
                    className={styles.linkBtn}
                    onClick={() => openPath(p)}
                  >
                    Open {p.split("/").pop()}
                  </button>
                ) : (
                  <code key={p} className={styles.bannerText}>
                    {p}
                  </code>
                ),
              )}
            </div>
          </div>
        )}
        {mode === "content" && save.kind === "error" && (
          <div
            className={`${styles.banner} ${styles.bannerBad}`}
            role="alert"
            data-save-error=""
          >
            <span className={styles.bannerTag}>Save // Not saved</span>
            <p className={styles.bannerText}>
              {sentence(save.message)} Your edits are still here.
            </p>
            {save.retry && (
              <div className={styles.bannerRow}>
                <button type="button" className={styles.ctrlBtn} onClick={saveNow}>
                  Try again
                </button>
              </div>
            )}
          </div>
        )}

        <div
          className={`${styles.viewerBody} ${mode === "content" && textFile ? styles.viewerBodyEditor : ""}`}
        >
          {mode === "diff" && diff.kind === "loading" && (
            <div className={styles.state} role="status">
              <span className={styles.stateTag}>Diff // Loading</span>
              Building the diff…
            </div>
          )}
          {mode === "diff" && diff.kind === "error" && (
            <div className={`${styles.state} ${styles.stateBad}`} role="alert">
              <span className={styles.stateTag}>Diff // Unavailable</span>
              {diff.message}
            </div>
          )}
          {/* Which two things are being compared is not guessable from the diff body, and for a
              conflict it is not the obvious pair — so it is stated rather than left implied. */}
          {mode === "diff" && diff.kind === "ok" && diff.d.conflict && (
            <div className={`${styles.state} ${styles.stateWarn}`}>
              <span className={styles.stateTag}>Diff // Conflict</span>
              Unresolved merge: this compares <strong>ours</strong> (removed
              lines) with <strong>theirs</strong> (added lines). The file on
              disk still has the merge markers — open CONTENT to see it.
            </div>
          )}
          {mode === "diff" && diff.kind === "ok" && diff.d.too_large && (
            <div className={`${styles.state} ${styles.stateWarn}`}>
              <span className={styles.stateTag}>Diff // Too large</span>
              This file is bigger than the panel will compare. Open CONTENT to
              read it instead.
            </div>
          )}
          {mode === "diff" && diff.kind === "ok" && diff.d.binary && (
            <div className={styles.state}>
              <span className={styles.stateTag}>Diff // Not text</span>
              This file is binary, or not valid UTF-8, so there is no meaningful
              line diff.
            </div>
          )}
          {mode === "diff" && diff.kind === "ok" && diff.d.coarse && (
            <div className={`${styles.state} ${styles.stateWarn}`}>
              <span className={styles.stateTag}>Diff // Coarse</span>
              These two versions are too different to line up cheaply, so the
              changed region is shown as a whole-block replacement rather than a
              line-by-line diff.
            </div>
          )}
          {mode === "diff" &&
            diff.kind === "ok" &&
            !diff.d.too_large &&
            !diff.d.binary && (
              <div className={styles.diffGrid} data-file-diff="">
                {parseDiff(diff.d.diff).map((h) => (
                  <div key={h.header} style={{ display: "contents" }}>
                    <span className={`${styles.diffNo}`} />
                    <span className={`${styles.diffNo}`} />
                    <span
                      className={`${styles.diffText}`}
                      style={{ color: "var(--text-3)" }}
                    >
                      {h.header}
                    </span>
                    {h.lines.map((l, i) => (
                      <div
                        key={`${h.header}:${i}`}
                        className={`${styles.diffLine} ${
                          l.kind === "add"
                            ? styles.diffAdd
                            : l.kind === "del"
                              ? styles.diffDel
                              : l.kind === "meta" || l.kind === "nonewline"
                                ? styles.diffMeta
                                : ""
                        }`}
                      >
                        <span className={styles.diffNo}>{l.oldNo ?? ""}</span>
                        <span className={styles.diffNo}>{l.newNo ?? ""}</span>
                        <span className={styles.diffText}>
                          {l.kind === "add"
                            ? "+"
                            : l.kind === "del"
                              ? "-"
                              : " "}
                          {l.text || " "}
                        </span>
                      </div>
                    ))}
                  </div>
                ))}
                {parseDiff(diff.d.diff).length === 0 && (
                  <div
                    className={styles.state}
                    style={{ gridColumn: "1 / -1" }}
                  >
                    <span className={styles.stateTag}>
                      Diff // No textual change
                    </span>
                    Nothing to show for this path.
                  </div>
                )}
              </div>
            )}
          {mode === "content" && state.kind === "loading" && (
            <div className={styles.state} role="status">
              <span className={styles.stateTag}>Viewer // Loading</span>
              Reading the file…
            </div>
          )}
          {mode === "content" && state.kind === "error" && (
            <div className={`${styles.state} ${styles.stateBad}`} role="alert">
              <span className={styles.stateTag}>Viewer // Unavailable</span>
              {state.message}
            </div>
          )}
          {mode === "content" && state.kind === "ok" && state.file.binary && (
            <div className={styles.state}>
              <span className={styles.stateTag}>Viewer // Binary file</span>
              {`${name} is binary (${state.file.mime ?? "unknown type"}, ${fmtBytes(state.file.size)}). Not rendered.`}
            </div>
          )}
          {mode === "content" && textFile && (
            <div className={styles.editorHost}>
              {/* The plain renderer stands in while the editor chunk downloads, so the file is
                  readable immediately rather than after a spinner. */}
              <Suspense
                fallback={
                  <div className={styles.code}>
                    {lines.map((line, i) => (
                      // Line order is stable for a given render; the index IS the identity here.
                      <div key={i} style={{ display: "contents" }}>
                        <span className={styles.lineNo}>{i + 1}</span>
                        <span className={styles.lineTxt}>{line || " "}</span>
                      </div>
                    ))}
                  </div>
                }
              >
                <CodeEditor
                  ref={attachEditor}
                  text={textFile.content ?? ""}
                  path={path}
                  editable={editorEditable}
                  label={`${name} contents`}
                  onDirtyChange={setDirty}
                  onSave={saveNow}
                  onCursor={(line, col) => setCursor({ line, col })}
                />
              </Suspense>
            </div>
          )}
        </div>

        <div className={styles.viewerFoot}>
          {/* The footer describes the mode you are actually looking at. It used to report the
              file's line count and size while the diff was on screen, which is a different fact
              about a different thing. */}
          <span className="hud-tag">
            {mode === "diff"
              ? diff.kind === "ok"
                ? diff.d.too_large
                  ? "TOO LARGE TO DIFF"
                  : diff.d.binary
                    ? "BINARY // NO LINE DIFF"
                    : diff.d.coarse
                      ? "COARSE // WHOLE-BLOCK REPLACEMENT"
                      : diff.d.conflict
                        ? // A conflict row opens with staged=false, so the working-tree/index
                          // wording would contradict the bytes actually being compared.
                          "OURS (STAGE 2) vs THEIRS (STAGE 3)"
                        : `${staged ? "INDEX" : "WORKING TREE"} vs ${staged ? "HEAD" : "INDEX"}`
                : "—"
              : state.kind === "ok"
                ? state.file.binary
                  ? "BINARY"
                  : `${languageLabel(path)} // UTF-8 // ${state.file.eol === "\r\n" ? "CRLF" : "LF"}${state.file.bom ? " // BOM" : ""} // ${fmtBytes(state.file.size)}`
                : "—"}
          </span>
          <span className="hud-tag" aria-live="polite">
            {mode === "diff" && diff.kind === "ok" && diff.d.truncated ? (
              "TRUNCATED // COUNTS WITHHELD"
            ) : mode === "content" && state.kind === "ok" && state.file.truncated ? (
              "TRUNCATED // FIRST 1 MB"
            ) : save.kind === "saving" ? (
              "SAVING…"
            ) : save.kind === "saved" && !dirty ? (
              <>
                {`SAVED ${new Date(save.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`}
                {save.retained && onOpenPath && (
                  <button
                    type="button"
                    className={styles.linkBtn}
                    onClick={() => openPath(save.retained!.path)}
                    data-open-previous=""
                  >
                    Open previous version
                  </button>
                )}
              </>
            ) : editing && cursor ? (
              `LN ${cursor.line}, COL ${cursor.col}`
            ) : (
              "ESC TO CLOSE"
            )}
          </span>
        </div>
      </div>
      {asking && (
        <>
          <button
            type="button"
            className={styles.confirmScrim}
            aria-label="Keep editing"
            onClick={keepEditing}
          />
          <div
            ref={confirmRef}
            className={styles.confirm}
            role="alertdialog"
            aria-modal="true"
            aria-label="Unsaved edits"
            data-close-confirm=""
          >
            <div className={styles.confirmHead}>
              <span className="hud-tag">Unsaved edits</span>
            </div>
            <p className={styles.confirmBody}>
              <strong>{name}</strong> has changes that are not saved.{" "}
              {pendingNav
                ? "Leaving this page"
                : confirmOpen !== null
                  ? "Opening another file"
                  : "Closing the viewer"}{" "}
              throws them away.
            </p>
            <div className={styles.confirmRow}>
              <button
                type="button"
                className={`${styles.ctrlBtn} ${styles.ctrlBad}`}
                onClick={discardAndLeave}
                data-discard-close=""
              >
                Discard edits
              </button>
              <button
                ref={keepRef}
                type="button"
                className={styles.cta}
                onClick={keepEditing}
                data-keep-editing=""
              >
                Keep editing
              </button>
            </div>
          </div>
        </>
      )}
    </>,
    document.body,
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
