import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  applyMotion,
  motionPaused,
  onMotionChange,
  prefersReducedMotion,
  readStoredMotion,
  REDUCED_MOTION_QUERY,
  resolveMotion,
  startMotionPause,
  storeMotion,
  type MotionChoice,
  type MotionPause,
} from "./motion";
import { MotionCtx } from "./motionStore";

/** Owns the motion mode (#1531, docs/design.md v2 §8). The initial value is the device cache,
 *  already applied pre-paint by the inline script in index.html. Unlike the theme there is no
 *  server seed: motion is a property of the device (a phone on battery, a desktop) the way the
 *  terminal size is. `setMotion` applies and caches at once, so a live switch tears loops down
 *  (and restores them) without a reload; it also owns the hidden-tab / idle pause. */
export function MotionProvider({ children }: { children: ReactNode }) {
  const [choice, setChoice] = useState<MotionChoice>(() => readStoredMotion());
  const [deviceReduced, setDeviceReduced] = useState(prefersReducedMotion);
  const [paused, setPaused] = useState<MotionPause | null>(() => motionPaused());
  const mode = resolveMotion(choice, deviceReduced);

  useEffect(() => {
    let mq: MediaQueryList | undefined;
    try {
      mq = window.matchMedia?.(REDUCED_MOTION_QUERY);
    } catch {
      return;
    }
    if (!mq) return;
    const onChange = () => setDeviceReduced(mq.matches);
    onChange();
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  useEffect(() => {
    applyMotion(mode);
  }, [mode]);

  // The pause attribute is owned by startMotionPause; React mirrors it for its own consumers.
  useEffect(() => {
    // Subscribe first, so the attribute startMotionPause sets at once is observed too.
    const off = onMotionChange(() => setPaused(motionPaused()));
    const stop = startMotionPause();
    return () => {
      off();
      stop();
    };
  }, []);

  const setMotion = useCallback((next: MotionChoice) => {
    setChoice(next);
    storeMotion(next);
  }, []);

  return (
    <MotionCtx.Provider
      value={{ mode, choice, paused, ambient: mode === "full" && paused === null, setMotion }}
    >
      {children}
    </MotionCtx.Provider>
  );
}
