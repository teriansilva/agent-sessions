/** The call to HQ (#1397 P1), above the router so a call survives navigation: the shell's on-call
 *  bar and the call panel read it on every route.
 *
 *  One `CallClient` per call: the Call tap makes a new one, so nothing from an ended call (its
 *  microphone, its socket) can come back. The page going away ends the call — the server would end
 *  it when the socket closes anyway; this says so first. Capabilities (can a call start, and if
 *  not, why) are read on mount, when a call ends, and when the tab comes back. */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { api, currentCsrfToken } from "../../lib/api";
import { ACTION_RESOLVED_EVENT } from "../../lib/actionEvents";
import { openAppSocket } from "../../lib/termSocket";
import type { VoiceCapabilities } from "../../types/voice";
import { useHqContact } from "../hq/useHqContact";
import { browserCallAudio, type CallAudioFactory } from "./callAudio";
import { CallClient, IDLE, type CallSnapshot } from "./callClient";
import { CallContext, type CallContextValue } from "./callContext";

const noClient = {
  subscribe: () => () => undefined,
  snapshot: IDLE,
};

export function CallProvider({
  children,
  audio = browserCallAudio,
}: {
  children: React.ReactNode;
  audio?: CallAudioFactory;
}) {
  const { wire } = useHqContact();
  const [client, setClient] = useState<CallClient | null>(null);
  const subscribe = useCallback(
    (fn: () => void) => (client ?? noClient).subscribe(fn),
    [client],
  );
  const snap: CallSnapshot = useSyncExternalStore(
    subscribe,
    () => (client ?? noClient).snapshot,
  );

  const [caps, setCaps] = useState<VoiceCapabilities | null>(null);
  // Only the NEWEST read lands (#1640 review 6916): a read started before a settings save that
  // answers after the save's own read would otherwise put the old availability back.
  const capsGen = useRef(0);
  const refreshCaps = useCallback(() => {
    const gen = ++capsGen.current;
    // Through a promise chain, so any failure (or a page without the voice routes) is a miss,
    // never an error thrown into rendering: no capabilities = no Call offered.
    Promise.resolve()
      .then(() => api.voiceCapabilities())
      .then((c) => {
        if (gen === capsGen.current) setCaps(c);
      })
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    refreshCaps();
    const onVisible = () => {
      if (document.visibilityState === "visible") refreshCaps();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [refreshCaps]);
  useEffect(() => {
    if (snap.phase === "ended") refreshCaps();
  }, [snap.phase, refreshCaps]);

  const trigger = useRef<HTMLElement | null>(null);
  const [minimized, setMinimized] = useState(false);
  // The setting (Settings → HQ calls) until the operator toggles captions during a call.
  const [captionsPick, setCaptionsOn] = useState<boolean | null>(null);
  const captionsOn = captionsPick ?? caps?.captions ?? true;

  const start = useCallback<CallContextValue["start"]>(
    (opts) => {
      if (client?.active) return;
      trigger.current = opts?.trigger ?? (document.activeElement as HTMLElement | null);
      const c = new CallClient({
        audio,
        admit: (body) => api.startCall(body),
        hangUp: (id) => api.endCall(id),
        openSocket: openAppSocket,
        csrf: currentCsrfToken,
      });
      setClient(c);
      setMinimized(false);
      void c.start({
        contact: wire === "off" ? null : wire,
        conversationId: opts?.conversationId ?? null,
      });
    },
    [client, audio, wire],
  );

  const dismiss = useCallback(() => {
    if (client?.active) return;
    client?.reset();
    setClient(null);
    setMinimized(false);
    const t = trigger.current;
    trigger.current = null;
    if (t && t.isConnected) t.focus();
  }, [client]);

  // A tap that decides an action ANYWHERE on the page (a Needs-you row, its details dialog)
  // closes the call's spoken window for that decision (#1681): the voice can't settle it twice.
  useEffect(() => {
    if (!client) return;
    const onResolved = (e: Event) => {
      const id = (e as CustomEvent<{ id?: unknown }>).detail?.id;
      if (typeof id === "string") client.cardDecided(id);
    };
    window.addEventListener(ACTION_RESOLVED_EVENT, onResolved);
    return () => window.removeEventListener(ACTION_RESOLVED_EVENT, onResolved);
  }, [client]);

  // Leaving the page ends the call (a reload, a sign-out's navigation, closing the tab).
  useEffect(() => {
    if (!client) return;
    const bye = () => client.end();
    window.addEventListener("pagehide", bye);
    return () => window.removeEventListener("pagehide", bye);
  }, [client]);

  const value = useMemo<CallContextValue>(
    () => ({
      snap,
      caps,
      refreshCaps,
      start,
      end: () => client?.end(),
      setMuted: (on: boolean) => client?.setMuted(on),
      cardDecided: (id: string) => client?.cardDecided(id),
      askNeedsYou: () => client?.askNeedsYou(),
      dismiss,
      minimized,
      setMinimized,
      captionsOn,
      setCaptionsOn,
      levels: () => ({ input: client?.inputLevel() ?? 0, output: client?.outputLevel() ?? 0 }),
      audioDevices: () => client?.deviceAudio() ?? null,
    }),
    [snap, caps, refreshCaps, start, client, dismiss, minimized, captionsOn],
  );
  return <CallContext.Provider value={value}>{children}</CallContext.Provider>;
}
