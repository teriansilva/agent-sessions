import styles from "../../routes/Settings.module.css";
import { useSubagentMode, type SubagentMode } from "./subagentMode";

const OPTIONS: { id: SubagentMode; label: string; description: string }[] = [
  { id: "nested", label: "Nested", description: "Under their main session, collapsed until opened" },
  { id: "hidden", label: "Hidden", description: "Only the ones that need you" },
];

/** Settings → Appearance → Interface → Subagent sessions (#2325): how the sidebar shows sessions
 *  another session started, on THIS device. Rarely changed, so it lives here rather than as a
 *  sidebar control. `nested` is the default; neither choice changes what search or filters match. */
export function SubagentModeSetting() {
  const [mode, setMode] = useSubagentMode();
  return (
    <>
      <h3 className={styles.subhead} id="subagent-sessions">
        Subagent sessions
      </h3>
      <p className={styles.hint}>
        How the sidebar lists sessions another session started. Search and filters only ever
        match main sessions.
      </p>
      <div
        className={styles.themes}
        role="radiogroup"
        aria-labelledby="subagent-sessions"
        data-testid="subagent-mode-setting"
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
            onClick={() => setMode(o.id)}
          >
            <span className={styles.themeName}>{o.label}</span>
            <span className={styles.themeDesc}>{o.description}</span>
          </button>
        ))}
      </div>
    </>
  );
}
