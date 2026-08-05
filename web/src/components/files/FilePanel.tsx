import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, Home, RefreshCw, X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { FileCapabilities, GitEntry, GitStatus } from "../../types/api";
import { FileTree } from "./FileTree";
import { GitTab } from "./GitTab";
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
}: {
  sessionKey: string;
  cwd: string;
  paneWidth: number;
  onClose: () => void;
  /** The control that opened the panel. Sheet mode is modal, so closing must hand focus back. */
  returnFocusTo?: HTMLElement | null;
}) {
  // Lazy initializer, not a ref read during render: seed the root from the persisted state once.
  const [root, setRoot] = useState<string>(() => loadPanelState(sessionKey)?.root || cwd);
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(loadPanelState(sessionKey)?.expanded ?? []),
  );
  // The SERVER's boundary, not a string guess: `parent` is null at the contained root.
  const [rootParent, setRootParent] = useState<string | null | undefined>(undefined);
  const [width, setWidth] = useState<number>(() => readStoredW());
  const [viewer, setViewer] = useState<{
    path: string;
    trigger: HTMLElement | null;
    modes?: { diff: boolean; content: boolean; defaultDiff: boolean };
    staged?: boolean;
  } | null>(null);
  const [tab, setTab] = useState<"files" | "git">("files");
  // Response is tagged with the tick it answers, so "loading" is DERIVED rather than set
  // synchronously inside the effect (which the compiler rightly rejects as a cascading render).
  const [gitRes, setGitRes] = useState<{
    tick: number;
    status: GitStatus | null;
    error: string | null;
  }>({ tick: -1, status: null, error: null });
  const [tick, setTick] = useState(0);
  const [caps, setCaps] = useState<FileCapabilities | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const sheetRef = useRef<HTMLDivElement>(null);

  const mode = panelMode(paneWidth);

  // Declared before the effects that use it: sheet mode is modal, so every close path — the
  // button, the scrim, Escape — must hand focus back to whatever opened the panel.
  const close = useCallback(() => {
    onClose();
    if (returnFocusTo && document.contains(returnFocusTo)) returnFocusTo.focus();
  }, [onClose, returnFocusTo]);

  useEffect(() => {
    let live = true;
    api
      .filesCapabilities()
      .then((c) => live && setCaps(c))
      .catch(() => live && setCaps({ ok: false, reason: "Could not reach the file service." }));
    return () => {
      live = false;
    };
  }, []);

  // Persist what the UI actually changes. `open` is written by SessionView (which outlives this
  // component — a panel that unmounts on close can never record `open: false` itself).
  useEffect(() => {
    savePanelState(sessionKey, { open: true, root, expanded: [...expanded] });
  }, [sessionKey, root, expanded]);

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

  // Sheet mode: lock body scroll and focus in, exactly like a modal (because it is one).
  useEffect(() => {
    if (mode !== "sheet") return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.body.style.overflow = prev;
    };
  }, [mode]);

  // Esc closes the sheet, and Tab is CONTAINED. Declaring `aria-modal` while letting focus walk
  // out to the background is a false claim — the viewer already had this trap; the sheet did not.
  // In dock mode the panel is not modal, so neither applies and Esc belongs to the terminal.
  useEffect(() => {
    if (mode !== "sheet") return;
    const onKey = (e: KeyboardEvent) => {
      // The viewer stacks above the sheet and owns the keyboard while it is open.
      if (document.querySelector("[data-file-viewer]")) return;
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
  }, [mode, close]);

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
      .then((s) => live && setGitRes({ tick, status: s, error: null }))
      .catch((e: unknown) => {
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setGitRes({
          tick,
          status: null,
          error: e instanceof ApiError ? e.message : "Could not read the repository.",
        });
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [root, tick]);

  const git = gitRes.status;
  const gitError = gitRes.error;
  const gitLoading = gitRes.tick !== tick;

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
    const out = [{ path: base, label: base.split("/").filter(Boolean).pop() || "/" }];
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
      const right = e.currentTarget.parentElement?.getBoundingClientRect().right ?? 0;
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
      const next = clampW(width + (e.key === "ArrowLeft" ? WIDTH_STEP : -WIDTH_STEP), paneWidth);
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
          title={canGoUp ? "Go to the parent folder" : "This is the top of the browsable root"}
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
      </div>

      {caps && !caps.ok ? (
        <div className={`${styles.state} ${styles.stateBad}`} role="alert">
          <span className={styles.stateTag}>Files // Unavailable</span>
          {caps.reason}
        </div>
      ) : tab === "git" ? (
        <GitTab
          status={git}
          loading={gitLoading}
          error={gitError}
          onRetry={() => setTick((n) => n + 1)}
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
        />
      )}

      <div className={styles.foot}>
        <span className="hud-tag">{root === cwd ? "ROOT // SESSION CWD" : "ROOT // CUSTOM"}</span>
        <span className="hud-tag">READ ONLY</span>
      </div>
    </>
  );

  if (mode === "sheet") {
    return (
      <>
        {createPortal(
          <>
            <button
              type="button"
              className={styles.sheetScrim}
              aria-label="Dismiss the file panel"
              onClick={close}
            />
            <div
              ref={sheetRef}
              className={styles.sheet}
              role="dialog"
              aria-modal="true"
              aria-label="Files"
              data-file-panel="sheet"
            >
              <div className={styles.grabber} aria-hidden="true" />
              {inner}
            </div>
          </>,
          document.body,
        )}
        {viewer && (
          <FileViewerModal
            key={`${viewer.path}:${viewer.staged ? 1 : 0}`}
            path={viewer.path}
            modes={viewer.modes}
            staged={viewer.staged}
            returnFocusTo={viewer.trigger}
            onClose={() => setViewer(null)}
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
      <aside className={styles.dock} style={{ width: w }} data-file-panel="dock" aria-label="Files">
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
        />
      )}
    </>
  );
}
