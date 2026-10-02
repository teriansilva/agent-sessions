import { useState } from "react";
import {
  readLinkOpenMode,
  writeLinkOpenMode,
  type LinkOpenMode,
} from "../../lib/linkOpenMode";
import { useIsMobile } from "../../lib/useIsMobile";
import styles from "../../routes/Settings.module.css";

const OPTIONS: { id: LinkOpenMode; label: string; description: string }[] = [
  { id: "ask", label: "Ask each time", description: "Full screen or map, per link" },
  { id: "fullscreen", label: "Full screen", description: "The session fills the Sessions pane" },
  { id: "map", label: "In map", description: "A window on the map, beside the others" },
];

/** Settings → Appearance → Opening links (#1232): how a session link from outside the app opens
 *  on THIS device. "Ask each time" is the reset. Not shown on a phone, where a link always opens
 *  full screen and there is nothing to choose. */
export function OpeningLinksSetting() {
  const isMobile = useIsMobile();
  const [mode, setMode] = useState<LinkOpenMode>(readLinkOpenMode);
  if (isMobile) return null;
  return (
    <>
      <h3 className={styles.subhead} id="opening-links">
        Opening links
      </h3>
      <p className={styles.hint}>
        How a BattleLab session link from outside the app opens on this device. Mission links
        always open in Missions.
      </p>
      <div
        className={styles.themes}
        role="radiogroup"
        aria-labelledby="opening-links"
        data-testid="opening-links"
      >
        {OPTIONS.map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={mode === o.id}
            className={
              mode === o.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard
            }
            onClick={() => {
              writeLinkOpenMode(o.id);
              setMode(o.id);
            }}
          >
            <span className={styles.themeName}>{o.label}</span>
            <span className={styles.themeDesc}>{o.description}</span>
          </button>
        ))}
      </div>
    </>
  );
}
