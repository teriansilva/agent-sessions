/** "Let mission control answer menus on its own", per mission (#1060 Phase 4).
 *
 *  The operator's opt-in to the one autonomous `choose`: when a session this mission holds stops at
 *  the engine's own option menu, mission control may pick an option itself. What it may NOT do is
 *  said beside the switch, because that is what the operator is agreeing to: never a permission
 *  prompt, only a menu read off the screen, only at 0.90 confidence or above, only at the yolo tier.
 *
 *  Turning it ON needs the tier the grant depends on; turning it OFF never does — withdrawing a
 *  grant must never be the control that is disabled. The server is the authority either way. */
import { useState } from "react";

import { useConfig } from "../../app/config";
import { ApiError, api } from "../../lib/api";
import type { Mission } from "../../types/api";

import styles from "./mission.module.css";

export function MissionAutoChoose({
  mission,
  onChanged,
}: {
  mission: Mission;
  onChanged: () => void;
}) {
  const orch = useConfig()?.orchestrator;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const server = mission.auto_choose === true;
  /** What the operator asked for, shown while it saves and ENDED BY THE FIRST FRESH SNAPSHOT AFTER
   *  THE SAVE SUCCEEDED (#1185 reviews 5374 and 5375, finding 4). Ending it on a change of the
   *  boolean was not enough: if another tab flips the value back before this tab's refetch lands,
   *  the refetch shows no change and the override would survive it. A new `mission` object after a
   *  successful save is the authoritative answer, whatever it says. Dropped on a refusal too, and
   *  keyed by mission at the mount, so it never carries to another mission. */
  const [pending, setPending] = useState<{
    value: boolean;
    saved: boolean;
  } | null>(null);
  const [seen, setSeen] = useState(mission);
  if (seen !== mission) {
    // React's "adjust state when a prop changes" pattern: during render, not in an effect.
    setSeen(mission);
    if (pending?.saved) setPending(null);
  }
  const on = pending ? pending.value : server;
  const yolo = !!orch?.enabled && orch.autonomy === "yolo";
  const canTurnOn = yolo;

  const toggle = async (next: boolean) => {
    setBusy(true);
    setError(null);
    setPending({ value: next, saved: false });
    try {
      await api.setMissionAutoChoose(mission.id, next);
      // From here the next snapshot of the mission is the truth, and ends the override.
      setPending({ value: next, saved: true });
      onChanged();
    } catch (e) {
      setPending(null);
      setError(
        e instanceof ApiError && e.message
          ? e.message
          : "That setting could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.autoChoose} data-testid="mission-auto-choose">
      <label className={styles.autoChooseRow}>
        <input
          type="checkbox"
          checked={on}
          disabled={busy || (!on && !canTurnOn)}
          onChange={(e) => void toggle(e.target.checked)}
          data-testid="mission-auto-choose-toggle"
        />
        <span>Answer the sessions’ menus on their own</span>
      </label>
      <p className={styles.autoChooseHint}>
        When a session in this mission stops at an option menu, mission control
        may pick an option itself — only from a menu it read off the screen,
        never a permission prompt, and only when it is at least 0.90 sure. Each
        answer is recorded in the thread.
      </p>
      {!yolo ? (
        <p
          className={styles.autoChooseHint}
          data-testid="mission-auto-choose-why"
        >
          {on
            ? "Paused: autonomy is not set to yolo, so every menu waits for your tap."
            : "Needs autonomy set to yolo in Settings → AI → Mission control."}
        </p>
      ) : null}
      {error ? (
        <p
          className={styles.objStale}
          role="alert"
          data-testid="mission-auto-choose-error"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
