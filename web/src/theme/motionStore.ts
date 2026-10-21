import { createContext, useContext } from "react";
import { DEFAULT_MOTION_CHOICE, type MotionChoice, type MotionMode, type MotionPause } from "./motion";

export interface MotionStore {
  /** The mode in force — what every animating consumer reads. */
  mode: MotionMode;
  /** What the operator chose, which may be `system`. Only the picker needs it. */
  choice: MotionChoice;
  /** Why ambient loops are stopped right now, or null. */
  paused: MotionPause | null;
  /** Full, and nothing paused it: the gate for ambient loops (canvas, glitch, portrait video). */
  ambient: boolean;
  setMotion: (choice: MotionChoice) => void;
}

/** Inert default (Full, no-op setter) so a consumer outside the provider degrades gracefully. */
export const MotionCtx = createContext<MotionStore>({
  mode: "full",
  choice: DEFAULT_MOTION_CHOICE,
  paused: null,
  ambient: true,
  setMotion: () => {},
});

export function useMotion(): MotionStore {
  return useContext(MotionCtx);
}
