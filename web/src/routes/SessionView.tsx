import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Terminal, type TerminalHandle } from "../components/terminal/Terminal";
import type { TemplateDraft } from "../components/terminal/Compose";
import { FilePanel } from "../components/files/FilePanel";
import { pathToken } from "../lib/pathToken";
import panel from "../components/files/filePanel.module.css";
import { useIsMobile } from "../lib/useIsMobile";
import { isNewSessionPlaceholder } from "../app/sessionsStore";
import { useSessionRow } from "../app/useSessionRow";
import { MAP_PATH, useMapWindows } from "../app/workspaceWindows";
import {
  loadPanelState,
  migratePanelState,
  savePanelState,
} from "../components/files/filePanelState";
import type { FreshSession } from "../lib/termUrl";
import { RuntimeGate } from "../components/terminal/RuntimeGate";

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
  // #905 P3: the gallery's USE lands here with a template staged in router state. It is read
  // ONCE, consumed out of the history entry (so a reload lands with nothing staged — the safe
  // failure), and handed to the composer's picker once the terminal has mounted.
  const stagedTemplateRef = useRef<string | undefined>(
    (location.state as { template?: string } | null)?.template,
  );

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

  const onSaveAsTemplate = useCallback(
    (draft: TemplateDraft) => navigate("/templates/new", { state: { prefill: draft } }),
    [navigate],
  );

  const onOpenGallery = useCallback((to: string) => navigate(to), [navigate]);

  useEffect(() => {
    const staged = stagedTemplateRef.current;
    if (!staged) return;
    // The history entry is cleared here, idempotently — a reload lands with nothing staged —
    // but the ref is consumed only when the callback actually RUNS: StrictMode's development
    // mount → cleanup → mount cancels the first frame, and a replay that found the ref already
    // empty opened the session without the picker (Hermes on #908, round 4).
    navigate(location.pathname, { replace: true, state: fresh ? { fresh } : null });
    const raf = requestAnimationFrame(() => {
      stagedTemplateRef.current = undefined;
      termRef.current?.openTemplates(staged);
    });
    return () => cancelAnimationFrame(raf);
    // Mount-only by design: the staging is consumed exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sessionKey = `${shown.engine}:${shown.id}`;
  // Look the row up under BOTH identities. The terminal identity stays frozen on the placeholder
  // so the live socket survives the opencode converge (#127) — but after `onReconcileId` the
  // session row exists only under the REAL id, and `fresh` has been dropped. Resolving panel
  // metadata from the frozen key alone therefore lost the cwd the moment the URL converged: the
  // Files action vanished and an open panel closed itself.
  //
  // #867: the sidebar's page is no longer the only source. When it doesn't hold this session —
  // a deep link, a reload after it fell off page 0, an archived one, one the list's visibility
  // scope hides — the accessor fetches the single row, so the Files trigger opens on the real
  // cwd instead of sitting disabled behind "this session has not reported a folder yet".
  const row = useSessionRow(sessionKey, liveKey);

  // "To map" (#936) — put this session on the map as a window and go there, the inverse of a
  // window's ⤢.
  //
  // It queues a REQUEST rather than opening anything: the anchor and the overlay box are facts
  // only a mounted, measured canvas has, so the map opens the window when it arrives. And it
  // asks under `liveKey` — the id the URL has settled on — never the frozen identity the terminal
  // still transports on, which after a converge names a session that no longer exists (#867).
  //
  // While the URL itself is STILL a `new-<uuid>` placeholder (a fresh opencode/codex launch that
  // has not reconciled yet) the chip is withheld entirely: a window on that id could only attach
  // to a session the server does not have. It appears a second later, when the converge lands.
  // Two gates, and they answer different questions.
  //
  // `useIsMobile` — the shell's own ≤800px breakpoint, not a second copy of it — because a phone
  // can never host a window, and because the pane head's action run has a MEASURED contract at
  // 420px (#744/#859: six icon-only chips fit and nothing folds) that a seventh chip breaks.
  //
  // `hostable` — the map's LAST measurement, because width alone does not establish map capacity:
  // a 1920×400 desktop window is wide enough and far too short, and offering the chip there sent
  // the operator to an empty map (Hermes on #939, finding 3). `mapReady` cannot serve: the map is
  // unmounted whenever this route is up, so it is always false. `null` (never measured) counts as
  // available — the drain hands an impossible request back to this route, so the cost of being
  // wrong is a round trip, and refusing an action on a map nobody has opened yet is worse.
  const isMobile = useIsMobile();
  const workspace = useMapWindows();
  const requestOpen = workspace?.requestOpen;
  const onToMap = useCallback(() => {
    if (!requestOpen || !engine || !id) return;
    requestOpen({
      key: liveKey,
      engine,
      id,
      title: row?.title || row?.short_uuid || id,
    });
    navigate(MAP_PATH);
  }, [requestOpen, engine, id, liveKey, row?.title, row?.short_uuid, navigate]);
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
        <RuntimeGate engine={shown.engine}>
          <Terminal
            ref={termRef}
            key={sessionKey}
            engine={shown.engine}
            id={shown.id}
            // The identity stays FROZEN (`key`/`engine`/`id`) so the live socket survives the
            // converge; `rowKey` carries the id the URL has settled on, purely so the header can
            // find the row — which only ever exists under the real id (#867).
            rowKey={liveKey}
            fresh={fresh}
            onReconcileId={onReconcileId}
            onSaveAsTemplate={onSaveAsTemplate}
            onOpenGallery={onOpenGallery}
            // Only the full-screen route passes this: a session already IN a window must not
            // offer to window itself (#936).
            onToMap={
              workspace &&
              !isMobile &&
              workspace.hostable !== false &&
              !isNewSessionPlaceholder(liveKey)
                ? onToMap
                : undefined
            }
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
        </RuntimeGate>
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
