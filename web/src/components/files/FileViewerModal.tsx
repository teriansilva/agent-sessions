import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { parseDiff } from "../../lib/diffParse";
import type { FileContent, GitDiff } from "../../types/api";
import styles from "./filePanel.module.css";

/** File viewer (#783) — an OVERLAY, never a pane split: opening a file must not evict, resize,
 *  or reflow the live terminal.
 *
 *  Portalled to <body> for two reasons, both load-bearing rather than stylistic. (1) `.terminal-pane`
 *  is `overflow: hidden`, so an in-tree overlay is clipped. (2) The terminal's coarse-pointer
 *  touch-capture layer sits at z-index 6 with `touch-action: none` and swallows touches — an
 *  overlay rendered inside that subtree is simply not tappable on a phone.
 *
 *  Dialog contract: `role="dialog"` + `aria-modal`, focus moved in on open, focus CONTAINED while
 *  open, focus returned to the trigger on close, Esc closes, and body scroll locked so the page
 *  behind cannot move under a touch drag. */
export function FileViewerModal({
  path,
  onClose,
  returnFocusTo,
  modes,
  staged = false,
}: {
  path: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
  /** Which modes apply to THIS row. A mode that does not apply is absent, never a dead control:
   *  a deleted file has nothing left to read, an untracked one nothing to compare against. */
  modes?: { diff: boolean; content: boolean; defaultDiff: boolean };
  staged?: boolean;
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

  // No synchronous `setState({kind:"loading"})` here: the parent keys this component by path, so
  // opening a different file REMOUNTS it and the initial state is already "loading".
  useEffect(() => {
    let live = true;
    const ctl = new AbortController();
    api
      .filesRead(path, { signal: ctl.signal })
      .then((file) => live && setState({ kind: "ok", file }))
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

  const close = useCallback(() => {
    onClose();
    // Return focus to whatever opened us — a11y, and it keeps keyboard tree navigation usable.
    if (returnFocusTo && document.contains(returnFocusTo))
      returnFocusTo.focus();
  }, [onClose, returnFocusTo]);

  // Focus in on open.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // Body scroll lock: without it a touch drag over the scrim scrolls the page behind the overlay.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  // Esc + focus containment.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      const root = panelRef.current;
      if (!root) return;
      const focusable = root.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !root.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [close]);

  const name = path.split("/").pop() || path;
  const lines =
    state.kind === "ok" && !state.file.binary
      ? (state.file.content ?? "").split("\n")
      : [];

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
      >
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

        {showDiff && showContent && (
          <div className={styles.modeBar} role="group" aria-label="View mode">
            <button
              type="button"
              className={`${styles.modeBtn} ${mode === "diff" ? styles.modeBtnOn : ""}`}
              aria-pressed={mode === "diff"}
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
            <span className={styles.modeSpacer} />
            <span className="hud-tag">
              {/* Withheld rather than guessed: the server nulls these when the diff was cut off,
                  because a count taken from a prefix is not a total. */}
              {diff.kind === "ok" &&
              diff.d.added !== null &&
              diff.d.removed !== null
                ? `+${diff.d.added} −${diff.d.removed}`
                : diff.kind === "ok" && diff.d.truncated
                  ? "COUNTS UNAVAILABLE"
                  : ""}
            </span>
          </div>
        )}

        <div className={styles.viewerBody}>
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
          {mode === "content" && state.kind === "ok" && !state.file.binary && (
            <div className={styles.code}>
              {lines.map((line, i) => (
                // Line order is stable for a given render; the index IS the identity here.
                <div key={i} style={{ display: "contents" }}>
                  <span className={styles.lineNo}>{i + 1}</span>
                  <span className={styles.lineTxt}>{line || " "}</span>
                </div>
              ))}
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
                  : `${lines.length} LINES // ${fmtBytes(state.file.size)}`
                : "—"}
          </span>
          <span className="hud-tag">
            {mode === "diff" && diff.kind === "ok" && diff.d.truncated
              ? "TRUNCATED // COUNTS WITHHELD"
              : mode === "content" &&
                  state.kind === "ok" &&
                  state.file.truncated
                ? "TRUNCATED // FIRST 1 MB"
                : "ESC TO CLOSE"}
          </span>
        </div>
      </div>
    </>,
    document.body,
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
