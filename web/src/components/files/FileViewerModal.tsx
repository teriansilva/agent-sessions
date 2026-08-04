import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { api, ApiError } from "../../lib/api";
import type { FileContent } from "../../types/api";
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
}: {
  path: string;
  onClose: () => void;
  returnFocusTo?: HTMLElement | null;
}) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ok"; file: FileContent } | { kind: "error"; message: string }
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
        if (!live || (e instanceof DOMException && e.name === "AbortError")) return;
        setState({
          kind: "error",
          message: e instanceof ApiError ? e.message : "Could not read this file.",
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
    if (returnFocusTo && document.contains(returnFocusTo)) returnFocusTo.focus();
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
  const lines = state.kind === "ok" && !state.file.binary ? (state.file.content ?? "").split("\n") : [];

  return createPortal(
    <>
      <button type="button" className={styles.viewerScrim} aria-label="Dismiss the file viewer" onClick={close} />
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

        <div className={styles.viewerBody}>
          {state.kind === "loading" && (
            <div className={styles.state} role="status">
              <span className={styles.stateTag}>Viewer // Loading</span>
              Reading the file…
            </div>
          )}
          {state.kind === "error" && (
            <div className={`${styles.state} ${styles.stateBad}`} role="alert">
              <span className={styles.stateTag}>Viewer // Unavailable</span>
              {state.message}
            </div>
          )}
          {state.kind === "ok" && state.file.binary && (
            <div className={styles.state}>
              <span className={styles.stateTag}>Viewer // Binary file</span>
              {`${name} is binary (${state.file.mime ?? "unknown type"}, ${fmtBytes(state.file.size)}). Not rendered.`}
            </div>
          )}
          {state.kind === "ok" && !state.file.binary && (
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
          <span className="hud-tag">
            {state.kind === "ok"
              ? state.file.binary
                ? "BINARY"
                : `${lines.length} LINES // ${fmtBytes(state.file.size)}`
              : "—"}
          </span>
          <span className="hud-tag">
            {state.kind === "ok" && state.file.truncated ? "TRUNCATED // FIRST 1 MB" : "ESC TO CLOSE"}
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
