import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, Home, RefreshCw, X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { FileCapabilities, GitEntry, GitStatus } from "../../types/api";
import { FileTree } from "./FileTree";
import { GitTab } from "./GitTab";
import { UploadControl } from "./UploadControl";
import { UploadQueue } from "./UploadQueue";
import { filesFromDataTransfer, type PlannedFile } from "./uploadPlan";
import { useUploads } from "./useUploads";
import { modesFor } from "./gitModes";
import { FileViewerModal } from "./FileViewerModal";
import { loadPanelState, savePanelState } from "./filePanelState";
import {
  DEFAULT_W,
  MIN_W,
  WIDTH_STEP,
  clampW,
  maxPanelW,
  panelMode,
  readStoredW,
  storeW,
} from "./filePanelLayout";
import styles from "./filePanel.module.css";

const POLL_MS = 15_000;

/** The session file panel (#783).
 *
 *  Two presentations, chosen by MEASUREMENT rather than a viewport breakpoint: a docked column
 *  when the pane can afford the panel *and* a usable terminal, a full-screen sheet otherwise. A
 *  narrow desktop pane therefore gets the sheet too — see `filePanelLayout.ts` for the arithmetic
 *  that makes the 800px rule wrong here.
 *
 *  The sheet is portalled to <body> and lands in the modal z-band. That is what makes it work on
 *  a phone at all: the terminal's coarse-pointer capture layer (z-index 6, `touch-action: none`)
 *  swallows touches for anything rendered beneath it inside the pane. */
export function FilePanel({
  sessionKey,
  cwd,
  paneWidth,
  onClose,
  returnFocusTo,
  onSendPath,
  contained = false,
  sheetKeyScopeActive = true,
  persistOpen = true,
}: {
  sessionKey: string;
  cwd: string;
  paneWidth: number;
  onClose: () => void;
  /** The control that opened the panel. Sheet mode is modal, so closing must hand focus back. */
  returnFocusTo?: HTMLElement | null;
  /** Absolute path → the compose draft (#792). Owned by SessionView, which is the only place
   *  holding both the session cwd and a handle that reaches Compose. */
  onSendPath?: (path: string) => void;
  /** Contained sheet (#1109): the host is a map window's body, and the sheet renders INSIDE it
   *  — inline, never portalled to <body>, no page scroll lock. A window-contained sheet covers
   *  its window, not the workspace; the phone reasons the portal exists for (the coarse-pointer
   *  touch layer, the modal z-band) do not apply to a desktop-only window's own DOM. */
  contained?: boolean;
  /** With `contained`: does THIS panel's window own the keyboard right now? A map window's
   *  background sheet must not touch the document — two windows' sheets each install a
   *  capture-phase keydown listener, and ungated, Escape in one closes BOTH (Hermes on
   *  #1109: focus inside the second window's sheet, both drawers vanished). `false` → the
   *  sheet installs NO document listener; its own in-sheet controls keep working, and the
   *  keys belong to whatever window IS active. The full-screen pane never passes it (it is
   *  the only pane on the page — always active). */
  sheetKeyScopeActive?: boolean;
  /** Whether to record `open: true` for this session (#783's reopen-on-reload). The full-screen
   *  pane persists it; a WINDOW drawer does not (#1109) — its drawer state is transient, and a
   *  window having files open should not force the next full-screen visit open too. The root
   *  and expanded set are still remembered either way (switching back should not lose your
   *  place). */
  persistOpen?: boolean;
}) {
  // Lazy initializer, not a ref read during render: seed the root from the persisted state once.
  const [root, setRoot] = useState<string>(
    () => loadPanelState(sessionKey)?.root || cwd,
  );
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(loadPanelState(sessionKey)?.expanded ?? []),
  );
  // The SERVER's boundary, not a string guess: `parent` is null at the contained root.
  const [rootParent, setRootParent] = useState<string | null | undefined>(
    undefined,
  );
  const [width, setWidth] = useState<number>(() => readStoredW());
  const [viewer, setViewer] = useState<{
    path: string;
    trigger: HTMLElement | null;
    modes?: { diff: boolean; content: boolean; defaultDiff: boolean };
    staged?: boolean;
  } | null>(null);
  const [tab, setTab] = useState<"files" | "git">("files");
  // Response is tagged with the tick AND the root it answers, so "loading" is DERIVED rather
  // than set synchronously inside the effect (which the compiler rightly rejects as a cascading
  // render).
  //
  // The root half is not symmetry for its own sake. A refresh bumps `tick`, so a stale response
  // was already ignored — but moving the panel to another folder does NOT bump the tick, so
  // between the navigation and the new status arriving `gitLoading` stayed false and the PREVIOUS
  // repository's rows kept rendering under the new `root`. Every control in the GIT tab targets
  // the current root, so a discard clicked on one of those rows sent the new root with the old
  // repo's path — and if both repositories have a file by that name, the server's fresh-path
  // check passes and the wrong repository's work is destroyed.
  const [gitRes, setGitRes] = useState<{
    root: string;
    tick: number;
    status: GitStatus | null;
    error: string | null;
  }>({ root: "", tick: -1, status: null, error: null });
  const [tick, setTick] = useState(0);
  const [caps, setCaps] = useState<FileCapabilities | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const sheetRef = useRef<HTMLDivElement>(null);

  const mode = panelMode(paneWidth);

  // Uploads (#807). The tree refreshes the affected directory when the batch settles, so the
  // panel shows what actually landed rather than what was asked for.
  const uploads = useUploads(() => setTick((n) => n + 1));

  /** Start a batch into `dir`. Entries are read from the DataTransfer synchronously by the
   *  caller; the walk itself is async and safe to await here. */
  const startDrop = useCallback(
    async (dir: string, dt: DataTransfer) => {
      let picked;
      try {
        picked = await filesFromDataTransfer(dt);
      } catch (e) {
        // `readEntries` can reject — an unreadable folder, a permission the browser withdraws
        // mid-walk. The caller discards this promise, so an uncaught rejection meant the drop
        // produced NOTHING: no queue, no refusal, no row. A traversal failure is an outcome and
        // has to be shown like any other.
        uploads.refuse(
          e instanceof Error && e.message
            ? `That folder could not be read: ${e.message}`
            : "That folder could not be read.",
        );
        return;
      }
      if (picked.length) await uploads.start(dir, picked);
      else uploads.refuse("Nothing in that drop could be read.");
    },
    [uploads],
  );

  const startPicked = useCallback(
    (picked: PlannedFile[]) => {
      void uploads.start(root, picked);
    },
    [uploads, root],
  );

  // Declared before the effects that use it: sheet mode is modal, so every close path — the
  // button, the scrim, Escape — must hand focus back to whatever opened the panel.
  const close = useCallback(() => {
    onClose();
    if (returnFocusTo && document.contains(returnFocusTo))
      returnFocusTo.focus();
  }, [onClose, returnFocusTo]);

  /** Send a path to the draft, then get out of the way if we are covering the draft (#792).
   *
   *  Keyed on the panel MODE rather than on pointer coarseness: the sheet is what sits over the
   *  compose box, and a narrow desktop pane gets the sheet too. Closing on "coarse pointer" would
   *  have left that case showing a confirmation the user cannot see, and would have closed a
   *  touch-driven DOCK that never covered anything. */
  const sendPath = useMemo(
    () =>
      onSendPath
        ? (path: string) => {
            onSendPath(path);
            if (mode === "sheet") close();
          }
        : undefined,
    [onSendPath, mode, close],
  );

  useEffect(() => {
    let live = true;
    api
      .filesCapabilities()
      .then((c) => live && setCaps(c))
      .catch(
        () =>
          live &&
          setCaps({ ok: false, reason: "Could not reach the file service." }),
      );
    return () => {
      live = false;
    };
  }, []);

  // Persist what the UI actually changes. `open` is written by SessionView (which outlives this
  // component — a panel that unmounts on close can never record `open: false` itself). A window
  // host passes `persistOpen: false` (#1109): the drawer's openness stays the window's own
  // transient state, and the remembered `open` flag is left exactly as it was.
  useEffect(() => {
    savePanelState(sessionKey, {
      open: persistOpen ? true : (loadPanelState(sessionKey)?.open ?? false),
      root,
      expanded: [...expanded],
    });
  }, [sessionKey, root, expanded, persistOpen]);

  // Poll only while the panel is open AND the document is visible — a backgrounded tab must not
  // keep spending worker budget.
  useEffect(() => {
    let timer: number | undefined;
    const schedule = () => {
      timer = window.setTimeout(() => {
        if (document.visibilityState === "visible") setTick((n) => n + 1);
        schedule();
      }, POLL_MS);
    };
    schedule();
    return () => window.clearTimeout(timer);
  }, []);

  // Sheet mode: focus in, exactly like a modal (because it is one) — contained or not.
  useEffect(() => {
    if (mode !== "sheet") return;
    closeRef.current?.focus();
  }, [mode]);
  // ...and lock the PAGE's scroll — only for the portalled sheet. A CONTAINED sheet (#1109)
  // lives inside a map window's body: there is no page scroll to lock, and locking it would
  // freeze the workspace behind the window.
  useEffect(() => {
    if (mode !== "sheet" || contained) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [mode, contained]);

  // Esc closes the sheet, and Tab is CONTAINED. Declaring `aria-modal` while letting focus walk
  // out to the background is a false claim — the viewer already had this trap; the sheet did not.
  // In dock mode the panel is not modal, so neither applies and Esc belongs to the terminal.
  useEffect(() => {
    if (mode !== "sheet") return;
    // A BACKGROUND window's sheet owns no keys (#1109): ungated, every open sheet's capture
    // listener fires and one Escape closes them all. Only the active window's sheet listens.
    if (contained && !sheetKeyScopeActive) return;
    const onKey = (e: KeyboardEvent) => {
      // The viewer stacks above the sheet and owns the keyboard while it is open — and so do the
      // upload menu and collision prompt (#807) and the GIT tab's branch menu and discard
      // confirmation (#806). Without this, Escape in any of them closed the whole PANEL: both
      // handlers are capture-phase on `document`, so registration order wins and this one is
      // registered first. Caught by the mobile e2e, invisible to jsdom. The list is a UNION —
      // the two features landed on separate branches and each added its own overlays.
      //
      // The GIT tab's confirmations are named by ONE shared marker, not one entry per dialog:
      // #950 added revert and unsettled-commit confirmations plus the touch row menu, and a
      // per-dialog list missed all three — Escape in any of them closed the panel.
      if (
        document.querySelector(
          "[data-file-viewer], [data-upload-menu], [data-collision-prompt], " +
            "[data-branch-menu], [data-row-menu], [data-git-confirm]",
        )
      )
        return;
      // And ANY OTHER aria-modal dialog portalled above the workspace — the session dialogs
      // this panel's owner chrome can open (brief, hand off, move to project, template save,
      // …). The panel cannot enumerate its owner's chrome, so the contract is STRUCTURAL: a
      // workspace-level dialog — not one inside a window (a background window's sheet is
      // focus-gated above and never owns keys), and not this sheet itself — owns the keyboard
      // while it is open. Escape belongs to the topmost dialog, not to the panel under it
      // (Hermes on #1109: Escape in the Session brief was closing the Files sheet beneath).
      const above = document.querySelector<HTMLElement>(
        '[role="dialog"][aria-modal="true"]:not([data-file-panel])',
      );
      if (above && !sheetRef.current?.contains(above) && !above.closest("[data-session-window]")) {
        return;
      }
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const root = sheetRef.current;
      if (!root) return;
      const focusable = Array.from(
        root.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
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
  }, [mode, close, contained, sheetKeyScopeActive]);

  // Up uses the listing's `parent`, which the server computes against the contained root and
  // returns as null when there is nowhere legal to go. Deriving it by trimming the string
  // produced "/home" at the default root and turned a valid view into an error on one click.
  const canGoUp = Boolean(rootParent);
  // Git status is fetched for the panel's root, on the same visibility-gated cadence as the tree.
  // It feeds BOTH the GIT tab and the status letters in the FILES tree, so browsing and reviewing
  // are one surface rather than two that disagree.
  useEffect(() => {
    let live = true;
    const ctl = new AbortController();
    api
      .gitStatus(root, { signal: ctl.signal })
      // `root` here is the one this request was ISSUED for, captured by the closure — not
      // whatever the panel is showing by the time it answers.
      .then((s) => live && setGitRes({ root, tick, status: s, error: null }))
      .catch((e: unknown) => {
        if (!live || (e instanceof DOMException && e.name === "AbortError"))
          return;
        setGitRes({
          root,
          tick,
          status: null,
          error:
            e instanceof ApiError
              ? e.message
              : "Could not read the repository.",
        });
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [root, tick]);

  // A status that answers for a different root is not "slightly stale", it is about a different
  // repository — so it is withheld entirely rather than rendered until it is replaced.
  const gitFresh = gitRes.root === root;
  const git = gitFresh ? gitRes.status : null;
  const gitError = gitFresh ? gitRes.error : null;
  const gitLoading = !gitFresh || gitRes.tick !== tick;

  const goUp = useCallback(() => {
    if (rootParent) {
      setRoot(rootParent);
      setRootParent(undefined);
    }
  }, [rootParent]);

  // Ancestor chain from the contained root down to the current root. `rootBase` comes from the
  // listing, so the chain can never offer a step outside the boundary the server enforces.
  const [rootBase, setRootBase] = useState<string | null>(null);
  const crumbs = (() => {
    const base = rootBase && root.startsWith(rootBase) ? rootBase : root;
    const rest = root.slice(base.length).split("/").filter(Boolean);
    const out = [
      { path: base, label: base.split("/").filter(Boolean).pop() || "/" },
    ];
    let acc = base;
    for (const seg of rest) {
      acc = `${acc}/${seg}`;
      out.push({ path: acc, label: seg });
    }
    // Keep the tail: the folders you are actually in.
    return out.length > 4 ? out.slice(-4) : out;
  })();

  const toggleExpanded = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // --- resize gutter: pointer drag + keyboard, mirroring the shipped sidebar separator ---
  const dragging = useRef(false);
  const onGutterDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);
  const onGutterMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!dragging.current) return;
      const right =
        e.currentTarget.parentElement?.getBoundingClientRect().right ?? 0;
      setWidth(clampW(right - e.clientX, paneWidth));
    },
    [paneWidth],
  );
  const onGutterUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      dragging.current = false;
      e.currentTarget.releasePointerCapture(e.pointerId);
      storeW(width);
    },
    [width],
  );
  const onGutterKey = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const next = clampW(
        width + (e.key === "ArrowLeft" ? WIDTH_STEP : -WIDTH_STEP),
        paneWidth,
      );
      setWidth(next);
      storeW(next);
    },
    [width, paneWidth],
  );

  const inner = (
    <>
      <div className={styles.head}>
        <span className={`hud-tag ${styles.headLabel}`}>FILES //</span>
        <span className={styles.headName} title={root}>
          {root.split("/").filter(Boolean).slice(-2).join("/") || root}
        </span>
        <button
          ref={closeRef}
          type="button"
          className={styles.iconBtn}
          onClick={close}
          aria-label="Close the file panel"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <div className={styles.tabs} role="tablist" aria-label="File panel tabs">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "files"}
          className={styles.tab}
          onClick={() => setTab("files")}
        >
          Files
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "git"}
          className={styles.tab}
          onClick={() => setTab("git")}
        >
          Git
          {git?.repo && git.entries.length > 0 && (
            <span className={styles.tabBadge}>{git.entries.length}</span>
          )}
        </button>
      </div>

      <div className={styles.crumbs}>
        <button
          type="button"
          className={styles.iconBtn}
          onClick={goUp}
          disabled={!canGoUp}
          aria-label="Go to the parent folder"
          title={
            canGoUp
              ? "Go to the parent folder"
              : "This is the top of the browsable root"
          }
        >
          <ArrowUp size={14} aria-hidden="true" />
        </button>
        {/* Real ancestor segments, each re-rooting to that directory. The previous single button
            just duplicated the reset beside it and offered no way to land on an intermediate
            folder. Bounded by the listing's own `root`, so it never offers a step outside it. */}
        <nav className={styles.crumbs2} aria-label="Folder path">
          {crumbs.map((c, i) => (
            <span key={c.path} className={styles.crumbItem}>
              {i > 0 && (
                <span className={styles.crumbSep} aria-hidden="true">
                  /
                </span>
              )}
              <button
                type="button"
                className={styles.crumbBtn}
                onClick={() => setRoot(c.path)}
                disabled={c.path === root}
                title={c.path}
              >
                {c.label}
              </button>
            </span>
          ))}
        </nav>
        <button
          type="button"
          className={styles.iconBtn}
          onClick={() => setRoot(cwd)}
          aria-label="Reset to the session folder"
        >
          <Home size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className={styles.iconBtn}
          onClick={() => setTick((n) => n + 1)}
          aria-label="Refresh"
        >
          <RefreshCw size={14} aria-hidden="true" />
        </button>
        {/* Only on the FILES tab: uploading targets the browsed directory, and the GIT tab is
            not browsing one. */}
        {tab === "files" && (
          <UploadControl disabled={uploads.busy || Boolean(caps && !caps.ok)} onFiles={startPicked} />
        )}
      </div>

      {caps && !caps.ok ? (
        <div className={`${styles.state} ${styles.stateBad}`} role="alert">
          <span className={styles.stateTag}>Files // Unavailable</span>
          {caps.reason}
        </div>
      ) : tab === "git" ? (
        <GitTab
          root={root}
          sessionKey={sessionKey}
          status={git}
          loading={gitLoading}
          error={gitError}
          onRetry={() => setTick((n) => n + 1)}
          onSendPath={sendPath}
          // A write answers with the post-write status, so the panel settles from the SERVER
          // rather than waiting up to a poll interval to notice its own change (#806). The root
          // the write STARTED against is carried through and re-checked here: a slow write in
          // repo A must not overwrite repo B's status after the operator moved the panel.
          onStatus={(s, forRoot) => {
            if (forRoot !== root) return;
            // Tagged with the root it belongs to, same as the poll: the guard above is what
            // rejects a stale write, and this is what keeps the state readable as "whose".
            setGitRes({ root, tick, status: s, error: null });
          }}
          onOpen={(e: GitEntry, trigger) =>
            setViewer({
              path: `${git?.repo ?? root}/${e.path}`,
              trigger,
              modes: modesFor(e),
              staged: e.kind === "staged",
            })
          }
        />
      ) : (
        <FileTree
          key={`${sessionKey}::${root}`}
          root={root}
          gitEntries={git?.entries ?? null}
          expanded={expanded}
          onToggleExpanded={toggleExpanded}
          onRootListing={(l) => {
            setRootParent(l.parent);
            setRootBase(l.root);
          }}
          refreshTick={tick}
          onOpenFile={(path, trigger) => setViewer({ path, trigger })}
          onSendPath={sendPath}
          onDropFiles={(dir, dt) => void startDrop(dir, dt)}
        />
      )}

      <UploadQueue uploads={uploads} />

      <div className={styles.foot}>
        <span className="hud-tag">
          {root === cwd ? "ROOT // SESSION CWD" : "ROOT // CUSTOM"}
        </span>
        {/* `READ ONLY` became a lie the moment upload (#807) and the git writes (#806) shipped,
            and `EDIT` left this list when the viewer became an editor (#950). What remains is
            still worth saying out loud. */}
        <span className="hud-tag">NO MOVE / RENAME / DELETE</span>
      </div>
    </>
  );

  if (mode === "sheet") {
    // A contained sheet (#1109) renders INLINE — the host (a map window's body) places it;
    // the portalled sheet is the viewport-sized one, and keeps every behaviour it shipped
    // with. The scrim and the sheet swap fixed→absolute via the contained modifier classes.
    const scrim = (
      <button
        type="button"
        className={contained ? `${styles.sheetScrim} ${styles.sheetInHost}` : styles.sheetScrim}
        aria-label="Dismiss the file panel"
        onClick={close}
      />
    );
    const sheet = (
      <div
        ref={sheetRef}
        className={contained ? `${styles.sheet} ${styles.sheetInHost}` : styles.sheet}
        role="dialog"
        aria-modal="true"
        aria-label="Files"
        data-file-panel="sheet"
      >
        <div className={styles.grabber} aria-hidden="true" />
        {inner}
      </div>
    );
    return (
      <>
        {contained ? (
          <>
            {scrim}
            {sheet}
          </>
        ) : (
          createPortal(
            <>
              {scrim}
              {sheet}
            </>,
            document.body,
          )
        )}
        {viewer && (
          <FileViewerModal
            key={`${viewer.path}:${viewer.staged ? 1 : 0}`}
            path={viewer.path}
            modes={viewer.modes}
            staged={viewer.staged}
            returnFocusTo={viewer.trigger}
            onClose={() => setViewer(null)}
            onOpenPath={(p) => setViewer({ path: p, trigger: viewer.trigger })}
          />
        )}
      </>
    );
  }

  const w = clampW(width || DEFAULT_W, paneWidth);
  return (
    <>
      <div
        className={styles.gutter}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the file panel"
        aria-valuenow={w}
        aria-valuemin={MIN_W}
        aria-valuemax={maxPanelW(paneWidth)}
        tabIndex={0}
        onPointerDown={onGutterDown}
        onPointerMove={onGutterMove}
        onPointerUp={onGutterUp}
        onDoubleClick={() => {
          setWidth(DEFAULT_W);
          storeW(DEFAULT_W);
        }}
        onKeyDown={onGutterKey}
      >
        <span className={styles.grip} aria-hidden="true" />
      </div>
      <aside
        className={styles.dock}
        style={{ width: w }}
        data-file-panel="dock"
        aria-label="Files"
      >
        {inner}
      </aside>
      {viewer && (
        <FileViewerModal
          key={`${viewer.path}:${viewer.staged ? 1 : 0}`}
          path={viewer.path}
          modes={viewer.modes}
          staged={viewer.staged}
          returnFocusTo={viewer.trigger}
          onClose={() => setViewer(null)}
          onOpenPath={(p) => setViewer({ path: p, trigger: viewer.trigger })}
        />
      )}
    </>
  );
}
