import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useSessionsStore } from "../../app/sessionsStore";
import { useMapWindows } from "../../app/workspaceWindows";
import { isStandalone, takeBootEntry } from "../../lib/bootEntry";
import { classifyLink, type LinkEntry } from "../../lib/linkEntry";
import { acceptLinks } from "../../lib/linkHandoff";
import { readLinkOpenMode, writeLinkOpenMode, type LinkOpenMode } from "../../lib/linkOpenMode";
import { MAP_PATH } from "../../lib/routes";
import { IN_APP_LINK_EVENT } from "../../lib/terminalLink";
import { useIsMobile } from "../../lib/useIsMobile";
import { OpenLinkDialog } from "./OpenLinkDialog";

type SessionEntry = Extract<LinkEntry, { kind: "session" }>;

interface LaunchParams {
  targetURL?: string;
}
interface LaunchQueue {
  setConsumer(fn: (params: LaunchParams) => void): void;
}

/** Where a BattleLab link that arrived from OUTSIDE the app ends up (#1232).
 *
 *  Four doors, one decision:
 *  - the document was opened on the link (`takeBootEntry`) — the route is already showing it;
 *  - the installed app was launched on it (`window.launchQueue`, `launch_handler:
 *    focus-existing`) — a cold launch is already showing it, a focused window is not. In the
 *    installed app this is the ONLY launch door: the boot door would see the same URL again;
 *  - another tab handed it over (`acceptLinks`);
 *  - it was clicked in agent output (`IN_APP_LINK_EVENT`, `lib/terminalLink`).
 *
 *  A mission link simply opens. A session link opens the way this device was told to (Settings →
 *  Appearance → Opening links), or asks — but only where a map window is possible at all: a phone
 *  and a map measured too small to host one always open full screen, and never ask. */
export function EntryLinks() {
  const navigate = useNavigate();
  const isMobile = useIsMobile();
  const workspace = useMapWindows();
  const requestOpen = workspace?.requestOpen;
  const hostable = workspace?.hostable ?? null;
  const { sessions } = useSessionsStore();
  // The pending prompt, and whether its session is ALREADY on screen (the boot door): then
  // "Full screen" and cancel are no-ops rather than a navigation.
  const [pending, setPending] = useState<{ entry: SessionEntry; onScreen: boolean } | null>(null);

  const openInMap = useCallback(
    (entry: SessionEntry, replace: boolean, title?: string) => {
      if (!requestOpen) {
        if (!replace) navigate(entry.path);
        return;
      }
      const key = `${entry.engine}:${entry.id}`;
      const row = sessions.find((s) => s.id === key);
      requestOpen({
        key,
        engine: entry.engine,
        id: entry.id,
        // The dialog's row, else the sidebar's; the map re-titles windows from its own list.
        title: title || row?.title || row?.short_uuid || entry.id,
      });
      navigate(MAP_PATH, { replace });
    },
    [requestOpen, navigate, sessions],
  );

  const handle = useCallback(
    (entry: LinkEntry, onScreen: boolean) => {
      // A link that ARRIVES while its own page is showing (a session printing its own link, the
      // app focused on the page it is already on) is already open. The boot door is different:
      // it asks about the page it was opened on, so it passes `onScreen` itself.
      if (!onScreen && window.location.pathname + window.location.search === entry.path) return;
      if (entry.kind === "mission" || isMobile || hostable === false || !requestOpen) {
        if (!onScreen) navigate(entry.path);
        return;
      }
      const mode = readLinkOpenMode();
      if (mode === "fullscreen") {
        if (!onScreen) navigate(entry.path);
      } else if (mode === "map") {
        openInMap(entry, onScreen);
      } else {
        setPending({ entry, onScreen });
      }
    },
    [isMobile, hostable, requestOpen, navigate, openInMap],
  );

  // The latest `handle`, for listeners registered once.
  const handleRef = useRef(handle);
  useEffect(() => {
    handleRef.current = handle;
  }, [handle]);

  const hasLaunchQueue = "launchQueue" in window;
  useEffect(() => {
    // Always take it, so no later reader acts on it; the installed app hands it to the launch
    // queue instead, which delivers the same URL.
    const boot = takeBootEntry();
    if (boot && !(isStandalone() && hasLaunchQueue)) handleRef.current(boot, true);
  }, [hasLaunchQueue]);

  useEffect(() => {
    const onLink = (e: Event) => handleRef.current((e as CustomEvent<LinkEntry>).detail, false);
    window.addEventListener(IN_APP_LINK_EVENT, onLink);
    return () => window.removeEventListener(IN_APP_LINK_EVENT, onLink);
  }, []);

  useEffect(() => acceptLinks((entry) => handleRef.current(entry, false)), []);

  // Registering from an effect is not late: the browser holds launch params until a consumer is
  // set, then delivers the pending one to it.
  useEffect(() => {
    const queue = (window as unknown as { launchQueue?: LaunchQueue }).launchQueue;
    if (!queue) return;
    queue.setConsumer((params) => {
      if (!params.targetURL) return;
      const entry = classifyLink(params.targetURL, window.location.origin);
      if (!entry) return;
      // A cold launch ON the link already rendered it; a reused window has not.
      const here = window.location.pathname + window.location.search;
      handleRef.current(entry, here === entry.path);
    });
  }, []);

  const onChoose = useCallback(
    (mode: Exclude<LinkOpenMode, "ask">, remember: boolean, title?: string) => {
      if (!pending) return;
      if (remember) writeLinkOpenMode(mode);
      setPending(null);
      if (mode === "map") openInMap(pending.entry, pending.onScreen, title);
      else if (!pending.onScreen) navigate(pending.entry.path);
    },
    [pending, openInMap, navigate],
  );

  if (!pending) return null;
  return (
    <OpenLinkDialog
      entry={pending.entry}
      onChoose={onChoose}
      onCancel={() => setPending(null)}
    />
  );
}
