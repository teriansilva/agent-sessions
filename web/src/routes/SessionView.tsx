import { useCallback, useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Terminal, type TerminalHandle } from "../components/terminal/Terminal";
import { FilePanel } from "../components/files/FilePanel";
import { pathToken } from "../lib/pathToken";
import panel from "../components/files/filePanel.module.css";
import { useSessionsStore } from "../app/sessionsStore";
import {
  loadPanelState,
  migratePanelState,
  savePanelState,
} from "../components/files/filePanelState";
import type { FreshSession } from "../lib/termUrl";

/** Session view at "/s/:engine/:id" — the URL is the single source of truth for which
 *  session is open (deep-linkable, refresh-safe). When arrived at from the new-session
 *  landing, router state carries the fresh-launch params (cwd + bypass) so the terminal
 *  opens it with ?new=1; a direct deep-link / reload has no state → a plain attach.
 *
 *  opencode new-session converge (#127): opencode mints its own id, so we open under a
 *  client placeholder (`/s/opencode/new-<uuid>`); when the server reconciles to the real
 *  `ses_…`, the terminal calls `onReconcileId` and we replace the URL to
 *  `/s/opencode/ses_…` (history replace, no reload) and DROP the fresh state (a later
 *  reload is now a plain attach by the real id). The displayed terminal keeps its ORIGINAL
 *  identity (the placeholder), so it is never remounted — the live socket is kept, no
 *  relaunch, no flicker.
 *
 *  File panel (#783): this view owns the pane LAYOUT — terminal, gutter, panel — because the
 *  panel is a sibling of the terminal rather than something inside it; the terminal only carries
 *  the trigger. The pane width is measured HERE rather than read off the viewport, because
 *  dock-vs-sheet depends on what this pane can actually spare (a 1400px viewport can hold a
 *  400px pane). */
export function SessionView() {
  const { engine, id } = useParams<{ engine: string; id: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const fresh = (location.state as { fresh?: FreshSession } | null)?.fresh;
  const liveKey = `${engine ?? ""}:${id ?? ""}`;

  // React Router reuses this component instance across a param-only change (no remount),
  // so we can't read the URL params directly for the terminal identity — our own
  // placeholder→real converge changes the params but must NOT re-key the terminal (that
  // would tear down the live socket). We hold the *displayed* identity in state and only
  // re-seed it on a genuine navigation. `converged` holds the real id(s) we ourselves
  // navigated to via reconcile; a param change matching one is OUR converge and keeps the
  // frozen identity. (A set, not a single value, so the keep-frozen decision is independent
  // of the order in which the param update and the state update commit.) It is cleared the
  // moment we adopt a genuine navigation, so the suppression is one-shot and scoped to the
  // still-mounted placeholder — a later navigation back to a reconciled real id re-opens it.
  const [shown, setShown] = useState({ engine: engine ?? "", id: id ?? "" });
  const [converged, setConverged] = useState<Set<string>>(() => new Set());

  // Derived-state reconciliation (render-safe): adopt the new params unless this is one of
  // our own converges. setState-during-render is the supported React pattern for adjusting
  // state to a prop change without an extra commit+effect round-trip.
  const shownKey = `${shown.engine}:${shown.id}`;
  if (liveKey !== shownKey && engine && id && !converged.has(liveKey)) {
    // A real navigation to a different session → show it (the new key remounts Terminal).
    setShown({ engine, id });
    // Drop the converge-suppression now that we've left the placeholder: it only ever guards
    // the in-place placeholder→real swap of the *currently shown* session. Without this, a
    // real id stayed in the set forever, so navigating away and later BACK to that same real
    // opencode URL would be wrongly treated as our converge and refused — the terminal would
    // stay on the other session while the address bar showed the opencode one (Hermes #131).
    if (converged.size) setConverged(new Set());
  }

  const onReconcileId = useCallback(
    (sid: string) => {
      // sid is the real engine-qualified id ("opencode:ses_…"); convert to the route.
      const [reEngine, ...rest] = sid.split(":");
      const reId = rest.join(":");
      if (!reEngine || !reId) return;
      // Remember this real id as OUR converge target so the upcoming param change keeps the
      // frozen terminal identity (live socket preserved). Replace the URL in place (no
      // history push, no reload) and drop the fresh-launch state so a subsequent reload
      // attaches by the real id rather than re-launching.
      setConverged((prev) => new Set(prev).add(`${reEngine}:${reId}`));
      navigate(`/s/${reEngine}/${reId}`, { replace: true, state: null });
    },
    [navigate],
  );

  const sessionKey = `${shown.engine}:${shown.id}`;
  const { sessions } = useSessionsStore();
  // Look the row up under BOTH identities. The terminal identity stays frozen on the placeholder
  // so the live socket survives the opencode converge (#127) — but after `onReconcileId` the
  // session row exists only under the REAL id, and `fresh` has been dropped. Resolving panel
  // metadata from the frozen key alone therefore lost the cwd the moment the URL converged: the
  // Files action vanished and an open panel closed itself.
  const row =
    sessions.find((s) => s.id === sessionKey) ??
    sessions.find((s) => s.id === liveKey);
  // The panel needs a real starting directory. A fresh launch carries one in router state before
  // the session row exists; otherwise it comes from the row. Until one of those is true the
  // trigger stays DISABLED rather than opening an empty tree — a session mid-reconcile has no
  // cwd yet, and an empty tree would read as "this folder is empty" (#783).
  // Remember the last cwd we resolved, KEYED ON THE SHOWN IDENTITY. `fresh` is cleared on
  // converge and the row can lag by a poll, so the panel would otherwise flicker out in the gap.
  // The key matters: an unkeyed cache survives a genuine A→B navigation, so if B's row has not
  // arrived yet, B's Files trigger would open against A's directory. `shown` is frozen across our
  // own placeholder→real converge and changes only on a real navigation — precisely the
  // invalidation rule this needs.
  const [seenCwd, setSeenCwd] = useState<{ key: string; cwd: string }>({
    key: sessionKey,
    cwd: "",
  });
  const resolvedCwd = row?.cwd || fresh?.cwd || "";
  if (seenCwd.key !== sessionKey)
    setSeenCwd({ key: sessionKey, cwd: resolvedCwd });
  else if (resolvedCwd && resolvedCwd !== seenCwd.cwd)
    setSeenCwd({ key: sessionKey, cwd: resolvedCwd });
  const cwd = resolvedCwd || (seenCwd.key === sessionKey ? seenCwd.cwd : "");

  // Panel state is persisted under the identity the URL has settled on. The TERMINAL keeps the
  // frozen placeholder (that is what preserves the live socket), but panel state must not: a
  // reload starts at the real id, so anything saved under the placeholder would be orphaned.
  // After our converge, `liveKey` is that real id and `converged` contains it.
  const panelKey = converged.has(liveKey) ? liveKey : sessionKey;

  // Remembered per session, reconciled DURING RENDER rather than in an effect — the same
  // derived-state pattern the converge logic above uses. A panel is never opened without a cwd to
  // point at, so a session still mid-reconcile shows a disabled trigger instead of an empty tree.
  // Store the PERSISTED intent only. Gating it on `cwd` here is wrong: on a fresh load the
  // sessions fetch has not landed, so `cwd` is empty on the first render and a remembered-open
  // panel would be latched shut forever. The cwd condition belongs at render time, below.
  const [files, setFiles] = useState(() => ({
    key: panelKey,
    open: Boolean(loadPanelState(panelKey)?.open),
  }));
  if (files.key !== panelKey) {
    // Migrate ONLY across the placeholder→real converge. `files.key !== panelKey` is also true
    // for ordinary A→B navigation, and migrating there moved A's open/root/expanded onto an
    // unseen B and deleted A's entry — then navigating back moved B's state onto A. The converge
    // edge is identifiable: `shown` is still the placeholder we froze, the live key is the id we
    // ourselves reconciled to, and the two differ.
    const isConvergeEdge =
      files.key === sessionKey &&
      sessionKey !== liveKey &&
      converged.has(liveKey);
    if (isConvergeEdge) migratePanelState(files.key, panelKey);
    setFiles({ key: panelKey, open: Boolean(loadPanelState(panelKey)?.open) });
  }
  const filesOpen = files.key === panelKey && files.open && Boolean(cwd);
  const setFilesOpen = useCallback((next: boolean) => {
    setFiles((prev) => {
      // Persisted HERE, not in FilePanel: the panel unmounts on close, so it can never record
      // `open: false` itself — which is why a closed panel used to reopen after a reload.
      const saved = loadPanelState(prev.key);
      savePanelState(prev.key, {
        open: next,
        root: saved?.root ?? null,
        expanded: saved?.expanded ?? [],
      });
      return { key: prev.key, open: next };
    });
  }, []);

  // The control that opened the panel, so sheet mode can hand focus back on close. State, not a
  // ref: it is read during render to build the panel's props.
  const [filesTrigger, setFilesTrigger] = useState<HTMLElement | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  // Reaches Compose (which lives inside Terminal) so a panel row can put a path in the draft.
  const termRef = useRef<TerminalHandle>(null);
  const [paneWidth, setPaneWidth] = useState(0);
  useLayoutEffect(() => {
    const el = rowRef.current;
    if (!el) return;
    const measure = () => setPaneWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!shown.engine || !shown.id) return null;
  return (
    <div className={panel.sessionRow} ref={rowRef}>
      <div className={panel.sessionTerm}>
        <Terminal
          ref={termRef}
          key={sessionKey}
          engine={shown.engine}
          id={shown.id}
          fresh={fresh}
          onReconcileId={onReconcileId}
          filesOpen={filesOpen}
          // Always present, even before the cwd resolves: #783 pins a VISIBLE DISABLED trigger
          // during reconciliation. Dropping the action made it vanish and reappear, which reads
          // as a glitch rather than as "not ready yet".
          filesDisabledReason={
            cwd ? undefined : "This session has not reported a folder yet"
          }
          onToggleFiles={(trigger?: HTMLElement | null) => {
            setFilesTrigger(trigger ?? null);
            setFilesOpen(!filesOpen);
          }}
        />
      </div>
      {filesOpen && cwd && (
        <FilePanel
          // Identity key: without it React reuses the mounted panel across a session change, so
          // A's root/expansions stayed in local state and the persistence effect then wrote them
          // under B's key — overwriting B even when migration correctly refused to.
          key={panelKey}
          sessionKey={panelKey}
          cwd={cwd}
          paneWidth={paneWidth}
          returnFocusTo={filesTrigger}
          onClose={() => setFilesOpen(false)}
          onSendPath={(path) => {
            // The panel knows the path; Compose knows the draft; neither knows the other. The
            // token is built here because this is the only place that holds BOTH the session cwd
            // (to relativise against) and the handle that reaches Compose.
            termRef.current?.insertToken(pathToken(path, cwd));
          }}
        />
      )}
    </div>
  );
}
