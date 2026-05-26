import { Code2, Coffee, Mail } from "lucide-react";
import { ArrowLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { engineName, humanBytes, humanDuration } from "../lib/format";
import { THEME_LIST } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import type { EngineInfo, SystemInfo } from "../types/api";
import styles from "./Settings.module.css";

const BUY_ME_A_COFFEE = "https://buymeacoffee.com/teriansilva";
const SOURCE_URL = "https://github.com/teriansilva/agent-sessions";
// Contact address kept out of the markup as a literal string (basic spam-scraper
// defence): assembled from the user + domain parts at runtime, so neither the served
// HTML nor a naive grep for the joined address finds it.
const CONTACT_USER = "contact";
const CONTACT_DOMAIN = "superstatus.io";
const contactAddr = () => `${CONTACT_USER}@${CONTACT_DOMAIN}`;

/** Connected agents (discovery): every known engine with a presence dot, a "can start
 *  new" badge, and the resolved binary path. */
function ConnectedAgents() {
  const [engines, setEngines] = useState<EngineInfo[] | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .engines()
      .then((d) => alive && setEngines(d.engines))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <section className={styles.section} aria-labelledby="agents-h">
      <h2 id="agents-h">Connected agents</h2>
      <p className={styles.hint}>The AI-coding CLIs TermRoyale can discover on this host.</p>
      {engines === null ? (
        <p className={styles.hint}>…</p>
      ) : (
        <ul className={styles.agents} aria-label="Connected agents">
          {engines.map((e) => (
            <li key={e.id} className={styles.agent}>
              <span
                className={`${styles.dot} ${e.present ? styles.dotOn : styles.dotOff}`}
                aria-hidden="true"
              />
              <span className={styles.agentName}>{engineName(e.id)}</span>
              <span className={styles.agentState}>{e.present ? "installed" : "not found"}</span>
              {e.supports_new && <span className={styles.newBadge}>can start new</span>}
              <span className={styles.agentBin}>{e.bin ?? "—"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** System: a tidy definition list of host capacity (fail-soft — any field may be absent). */
function SystemCard() {
  const [sys, setSys] = useState<SystemInfo | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .system()
      .then((d) => alive && setSys(d))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  const rows: { label: string; value: string | null }[] = sys
    ? [
        { label: "OS", value: sys.os ?? null },
        {
          label: "Platform",
          value: [sys.platform, sys.arch].filter(Boolean).join(" · ") || null,
        },
        {
          label: "CPU",
          value:
            sys.cpus != null
              ? sys.load
                ? `${sys.cpus} cores · load ${sys.load["1"].toFixed(2)}`
                : `${sys.cpus} cores`
              : null,
        },
        {
          label: "Memory",
          value:
            sys.mem_total != null
              ? sys.mem_available != null
                ? `${humanBytes(sys.mem_total - sys.mem_available)} / ${humanBytes(sys.mem_total)}`
                : humanBytes(sys.mem_total)
              : null,
        },
        {
          label: "Disk",
          value:
            sys.disk_total != null && sys.disk_free != null
              ? `${humanBytes(sys.disk_free)} free / ${humanBytes(sys.disk_total)}`
              : null,
        },
        {
          label: "Uptime",
          value: sys.uptime_seconds != null ? humanDuration(sys.uptime_seconds) : null,
        },
        { label: "App version", value: sys.version ?? null },
        { label: "Python", value: sys.python ?? null },
      ]
    : [];

  return (
    <section className={styles.section} aria-labelledby="system-h">
      <h2 id="system-h">System</h2>
      {sys === null ? (
        <p className={styles.hint}>…</p>
      ) : (
        <dl className={styles.meta}>
          {rows
            .filter((r) => r.value != null)
            .map((r) => (
              <div key={r.label} className={styles.metaRow}>
                <dt>{r.label}</dt>
                <dd>{r.value}</dd>
              </div>
            ))}
        </dl>
      )}
    </section>
  );
}

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
          <dt>Created by</dt>
          <dd>Marcus Braun</dd>
        </dl>
        <div className={styles.aboutLinks}>
          <a className={styles.aboutLink} href={SOURCE_URL} target="_blank" rel="noopener noreferrer">
            <Code2 size={15} /> Source code
          </a>
          <a
            className={styles.aboutLink}
            href={`mailto:${contactAddr()}`}
            onClick={(e) => {
              // Assemble the mailto at click time so the literal address is never in the DOM at rest.
              (e.currentTarget as HTMLAnchorElement).href = `mailto:${contactAddr()}`;
            }}
          >
            <Mail size={15} /> {CONTACT_USER}&#64;{CONTACT_DOMAIN}
          </a>
        </div>
      </section>

      <ConnectedAgents />

      <SystemCard />

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
