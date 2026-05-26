import { ArrowLeft, Coffee } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { THEME_LIST } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import styles from "./Settings.module.css";

const BUY_ME_A_COFFEE = "https://buymeacoffee.com/teriansilva";

/** Settings (#109): theme picker (applies app-wide + to the terminal), an About section
 *  with the running version, and a support link. Reached via the gear in the sidebar. */
export function Settings() {
  const { theme, setTheme } = useTheme();
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setVersion(v.version))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div className={styles.wrap}>
      <header className={styles.head}>
        <Link to="/" className={styles.back} aria-label="Back to sessions">
          <ArrowLeft size={18} />
        </Link>
        <h1>Settings</h1>
      </header>

      <section className={styles.section} aria-labelledby="appearance-h">
        <h2 id="appearance-h">Appearance</h2>
        <p className={styles.hint}>Choose how TermRoyale looks.</p>
        <div className={styles.themes} role="radiogroup" aria-label="Theme">
          {THEME_LIST.map((t) => (
            <button
              key={t.id}
              type="button"
              role="radio"
              aria-checked={theme === t.id}
              className={theme === t.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard}
              onClick={() => setTheme(t.id)}
            >
              <span className={`${styles.swatch} ${styles[`sw_${t.id}`]}`} aria-hidden="true" />
              <span className={styles.themeName}>{t.label}</span>
              <span className={styles.themeDesc}>{t.description}</span>
            </button>
          ))}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="about-h">
        <h2 id="about-h">About</h2>
        <p className={styles.brandLine}>
          👑 Term<b>Royale</b>
        </p>
        <p className={styles.hint}>Finally, a terminal with some class.</p>
        <p className={styles.blurb}>
          The mobile-first organizer for your AI-coding sessions — claude, opencode, codex and
          gemini, all in one place.
        </p>
        <dl className={styles.meta}>
          <dt>Version</dt>
          <dd>{version ?? "…"}</dd>
        </dl>
      </section>

      <section className={styles.section} aria-labelledby="support-h">
        <h2 id="support-h">Support</h2>
        <p className={styles.blurb}>If TermRoyale saves you time, you can support its development.</p>
        <a
          className={`${styles.coffee} shine`}
          href={BUY_ME_A_COFFEE}
          target="_blank"
          rel="noopener noreferrer"
        >
          <Coffee size={16} /> Buy me a coffee
        </a>
      </section>
    </div>
  );
}
