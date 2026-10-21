import { useState, type CSSProperties } from "react";
import { Link, useLocation } from "react-router-dom";
import { HqContactGallery } from "../components/hq/HqContactGallery";
import { OpeningLinksSetting } from "../components/links/OpeningLinksSetting";
import { onRadioGroupKeyDown, rovingTabIndex } from "../lib/radioArrows";
import {
  accentPresetFor,
  ACCENT_PRESETS,
  DEFAULT_ACCENT,
  normalizeAccent,
  onAccentFor,
} from "../theme/accent";
import { useAccent } from "../theme/accentStore";
import type { ThemeChoice, ThemeId } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import { Seg } from "../components/hud/Controls";
import type { MotionChoice } from "../theme/motion";
import { useMotion } from "../theme/motionStore";
import a from "./Appearance.module.css";
import styles from "./Settings.module.css";
import { settingsPath } from "./settingsTabs";

const MODES: { id: ThemeChoice; label: string; description: string }[] = [
  { id: "dark", label: "Dark", description: "Near-black tactical HUD" },
  { id: "light", label: "Light", description: "Paper-grey daylight" },
  {
    id: "system",
    label: "Match device",
    description: "Follows your OS, switches live",
  },
];

/** The motion choices (#1531, docs/design.md v2 §8). `system` follows the device's
 *  prefers-reduced-motion (Reduced when it asks, else Full). */
const MOTIONS: { value: MotionChoice; label: string; description: string }[] = [
  { value: "system", label: "Match device", description: "Follows your device's reduce-motion setting." },
  {
    value: "full",
    label: "Full",
    description: "Ambient scanlines and pulses, boot sequence, arrivals, streaming and transitions.",
  },
  {
    value: "reduced",
    label: "Reduced",
    description: "Short fades on state changes only. No loops, no boot, no video; LEDs hold steady.",
  },
  { value: "off", label: "Off", description: "Nothing moves. The look stays complete." },
];
const MODE_WORD = { full: "Full", reduced: "Reduced", off: "Off" } as const;

/** A miniature of the app in one mode: sidebar, title, a panel with a CTA (no status LED — a preview is not a state, design §3). The
 *  ground is the mode's own (fixed — a card must show ITS mode, not the one in force), while the
 *  brand bits take the live accent, so the preview is what you would actually get. */
function MiniApp({ mode }: { mode: ThemeId }) {
  return (
    <span className={`${a.mini} ${a[`mini_${mode}`]}`} aria-hidden="true">
      <span className={a.miniSide}>
        <span className={`${a.bar} ${a.barAccent}`} style={{ width: "70%" }} />
        <span className={a.bar} style={{ width: "85%" }} />
        <span className={a.bar} style={{ width: "60%" }} />
        <span className={a.bar} style={{ width: "75%" }} />
      </span>
      <span className={a.miniMain}>
        <span className={`${a.bar} ${a.barStrong}`} style={{ width: "40%" }} />
        <span className={a.miniPanel}>
          <span className={`${a.bar} ${a.barStrong}`} style={{ width: "80%" }} />
          <span className={a.bar} style={{ width: "62%" }} />
          <span className={a.miniFoot}>
            <span className={a.bar} style={{ width: "40%" }} />
            <span className={a.miniCta} />
          </span>
        </span>
      </span>
    </span>
  );
}

/** Settings → Appearance → Interface (#1494): how BattleLab itself looks — mode, motion (#1531), accent, the
 *  primary HQ contact (#1525), and how outside links open. The terminal and API-session looks have their own pages. */
export function AppearanceInterface() {
  const { choice, setTheme } = useTheme();
  const { accent, setAccent } = useAccent();
  const motion = useMotion();
  const motionDesc = MOTIONS.find((m) => m.value === motion.choice)?.description ?? "";
  // Draft for the free-text hex field — committed on Enter/blur so mid-typing (e.g. a
  // transient valid #rgb prefix) doesn't churn the live accent or the server. When the
  // accent changes elsewhere (a preset, the colour well, another device) we reflect it into
  // the field via React's render-phase "adjust state on change" pattern (no effect needed).
  const [hexDraft, setHexDraft] = useState(accent);
  const [syncedAccent, setSyncedAccent] = useState(accent);
  if (accent !== syncedAccent) {
    setSyncedAccent(accent);
    setHexDraft(accent);
  }
  const commitHex = () => {
    const norm = normalizeAccent(hexDraft);
    if (norm) setAccent(norm);
    else setHexDraft(accent); // reset an invalid entry back to the active accent
  };
  const preset = accentPresetFor(accent);
  // Forwarded on the in-page link exactly as the sidebar forwards it, so the validated `returnTo`
  // (where the gear was tapped from) survives the hop and Back still lands there.
  const location = useLocation();

  return (
    <section className={styles.section} aria-labelledby="appearance-h">
      <h2 id="appearance-h">Interface</h2>
      <p className={styles.hint}>
        How BattleLab itself looks: the frame, menus, buttons and panels.
      </p>

      <h3 className={styles.subhead} id="mode-h">
        Mode
      </h3>
      <div
        className={a.modes}
        role="radiogroup"
        aria-labelledby="mode-h"
        onKeyDown={onRadioGroupKeyDown}
      >
        {MODES.map((m, i) => {
          const on = choice === m.id;
          return (
            <button
              key={m.id}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={rovingTabIndex(on, i, MODES.some((x) => x.id === choice))}
              className={on ? `${a.mode} ${a.on}` : a.mode}
              onClick={() => setTheme(m.id)}
            >
              <span className={a.modePreview}>
                {m.id === "system" ? (
                  <>
                    <MiniApp mode="dark" />
                    <MiniApp mode="light" />
                  </>
                ) : (
                  <MiniApp mode={m.id} />
                )}
              </span>
              <span className={a.modeLabel}>
                <span className={a.radio} aria-hidden="true" />
                <span className={a.modeText}>
                  <span className={a.modeName}>{m.label}</span>
                  <span className={a.modeDesc}>{m.description}</span>
                </span>
              </span>
            </button>
          );
        })}
      </div>

      <h3 className={styles.subhead} id="motion-h">
        Motion
      </h3>
      <p className={styles.hint}>
        Animation in the frame: never on the terminal, the editor or text you are typing, and never
        delaying input. Saved on this device.
      </p>
      <Seg
        label="Motion"
        options={MOTIONS.map(({ value, label }) => ({ value, label }))}
        value={motion.choice}
        onChange={motion.setMotion}
        testId="motion-seg"
      />
      <p className={styles.hint} data-testid="motion-desc" aria-live="polite">
        {motionDesc}
        {motion.choice === "system" ? ` Now: ${MODE_WORD[motion.mode]}.` : ""}
      </p>

      <h3 className={styles.subhead} id="accent-h">
        Accent
      </h3>
      <p className={styles.hint}>
        The brand colour: buttons, highlights, selection and the terminal cursor.
      </p>
      <div
        className={a.swatches}
        role="radiogroup"
        aria-labelledby="accent-h"
        onKeyDown={onRadioGroupKeyDown}
      >
        {ACCENT_PRESETS.map((p, i) => (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={accent === p.hex}
            tabIndex={rovingTabIndex(accent === p.hex, i, !!preset)}
            aria-label={p.label}
            title={p.label}
            className={accent === p.hex ? `${a.swatch} ${a.on}` : a.swatch}
            style={
              { "--dot": p.hex, "--dot-ink": onAccentFor(p.hex) } as CSSProperties
            }
            onClick={() => setAccent(p.hex)}
          />
        ))}
      </div>
      <div className={a.customRow}>
        <label className={a.well} title="Custom colour">
          <input
            type="color"
            aria-label="Custom accent colour"
            value={accent}
            onChange={(e) => setAccent(e.target.value)}
          />
        </label>
        <input
          type="text"
          inputMode="text"
          spellCheck={false}
          className={a.hex}
          aria-label="Accent hex value"
          value={hexDraft}
          onChange={(e) => setHexDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitHex();
            }
          }}
          onBlur={commitHex}
        />
        <button
          type="button"
          className={a.reset}
          disabled={accent === DEFAULT_ACCENT}
          onClick={() => setAccent(DEFAULT_ACCENT)}
        >
          Reset
        </button>
        <span className={a.chosen} data-testid="accent-name">
          {preset ? preset.label : "Custom"}
          {accent === DEFAULT_ACCENT ? " · default" : ""}
        </span>
      </div>

      <HqContactGallery subheadClass={styles.subhead} hintClass={styles.hint} />

      <OpeningLinksSetting />

      <p className={a.moved}>
        The terminal&rsquo;s colours, font and size are under{" "}
        <Link to={settingsPath("terminal-appearance")} state={location.state}>
          Terminal sessions
        </Link>
        ; how API-session conversations read is under{" "}
        <Link to={settingsPath("api-appearance")} state={location.state}>
          API sessions
        </Link>
        .
      </p>
    </section>
  );
}
