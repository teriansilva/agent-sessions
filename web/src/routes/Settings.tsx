import encodeQR from "@paulmillr/qr";
import {
  Archive,
  ArrowLeft,
  Code2,
  Coffee,
  Copy,
  Download,
  LogOut,
  Mail,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import {
  type CSSProperties,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { useConfig, useConfigRefresh } from "../app/config";
import { EnableLoginDetails } from "../components/EnableLoginDetails";
import { useOverviewPrefs } from "../app/overviewPrefs";
import { api, ApiError } from "../lib/api";
import { engineName, humanBytes, humanDuration, shortCwd } from "../lib/format";
import { buildProjectTree, flattenTree, owningProjectId } from "../lib/projectTree";
import { FolderPickerModal } from "../components/FolderPickerModal";
import { AiActivityPanel } from "./AiActivityPanel";
import { AiReviewSettings } from "./AiReviewSettings";
import { AutoSortSettings } from "./AutoSortSettings";
import { PulseSettings } from "./PulseSettings";
import { ProjectsManagerCard } from "./ProjectsManager";
import { RenameProjectModal } from "./RenameProjectModal";
import { ACCENT_PRESETS, normalizeAccent } from "../theme/accent";
import { useAccent } from "../theme/accentStore";
import { THEME_LIST } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import type {
  EngineInfo,
  ProjectEntity,
  SystemInfo,
  TwoFactorEnrollment,
  UpdateInfo,
  UpdateSettings,
} from "../types/api";
import {
  DEFAULT_SETTINGS_TAB,
  isSettingsTab,
  SETTINGS_TABS,
  type SettingsTabId,
} from "./settingsTabs";
import styles from "./Settings.module.css";

const BUY_ME_A_COFFEE = "https://buymeacoffee.com/teriansilva";
const SOURCE_URL = "https://github.com/teriansilva/agent-sessions";
// Contact address kept out of the markup as a literal string (basic spam-scraper
// defence): assembled from the user + domain parts at runtime, so neither the served
// HTML nor a naive grep for the joined address finds it.
const CONTACT_USER = "contact";
const CONTACT_DOMAIN = "superstatus.io";
const contactAddr = () => `${CONTACT_USER}@${CONTACT_DOMAIN}`;

/** Keyboard-accessible tablist (WAI-ARIA tabs pattern): roving tabindex, ArrowLeft/Right
 *  with wrap-around, Home/End, selection follows focus. Switching tabs navigates to the
 *  canonical `/settings/:tab` URL; the router state (the #155 returnTo) rides along so the
 *  back button keeps working across tab switches. */
function SettingsTablist({ active }: { active: SettingsTabId }) {
  const navigate = useNavigate();
  const location = useLocation();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const select = (index: number, opts?: { replace?: boolean }) => {
    const tab = SETTINGS_TABS[index];
    refs.current[index]?.focus();
    if (tab.id !== active) {
      navigate(`/settings/${tab.id}`, { replace: opts?.replace, state: location.state });
    }
  };

  const onKeyDown = (e: KeyboardEvent, index: number) => {
    const last = SETTINGS_TABS.length - 1;
    let next: number | null = null;
    if (e.key === "ArrowRight") next = index === last ? 0 : index + 1;
    else if (e.key === "ArrowLeft") next = index === 0 ? last : index - 1;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = last;
    if (next === null) return;
    e.preventDefault();
    // Keyboard roving replaces the history entry so arrowing across the bar doesn't
    // stack one entry per keypress.
    select(next, { replace: true });
  };

  return (
    <nav className={styles.tabs} role="tablist" aria-label="Settings sections">
      {SETTINGS_TABS.map((t, i) => (
        <button
          key={t.id}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="tab"
          id={`settings-tab-${t.id}`}
          aria-selected={t.id === active}
          aria-controls={`settings-panel-${t.id}`}
          tabIndex={t.id === active ? 0 : -1}
          className={t.id === active ? `${styles.tab} ${styles.active}` : styles.tab}
          onClick={() => select(i)}
          onKeyDown={(e) => onKeyDown(e, i)}
        >
          {t.label}
        </button>
      ))}
    </nav>
  );
}

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
      <p className={styles.hint}>The AI-coding CLIs BattleLab can discover on this host.</p>
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

/** Updates: compare the running version to the channel's latest and apply (re-runs the
 *  installer). Only meaningful for installer-managed deploys; in a dev/source checkout
 *  apply returns 503 (surfaced). On the default `stable` channel with no release tags
 *  yet, the check reports "up to date" (no `latest`).
 *
 *  #538: the card also owns the persisted update settings — the daily automatic-update
 *  toggle and the release channel. Both load from the cheap `/api/update/settings` (no
 *  remote hit on mount) and save optimistically; the server applies them live. The
 *  last-automatic-check line is recent runtime status only (in-memory server-side —
 *  it resets when the service restarts). */
function UpdatesCard() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [settings, setSettings] = useState<UpdateSettings | null>(null);
  const [state, setState] = useState<"idle" | "checking" | "applying" | "applied" | "error">(
    "idle",
  );
  const [msg, setMsg] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped on every channel switch: an in-flight check that started under the previous
  // channel must not repopulate `info` under the new one (Hermes #539 race).
  const checkGen = useRef(0);

  // Show the running version + persisted settings immediately; the remote compare (a git
  // ls-remote) only runs when the user clicks "Check for updates".
  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setCurrent(v.version))
      .catch(() => {});
    api
      .updateSettings()
      .then((s) => alive && setSettings(s))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const save = async (patch: { auto_update?: boolean; channel?: string }) => {
    const prev = settings;
    if (prev) setSettings({ ...prev, ...patch });
    if (patch.channel) {
      // Invalidate the previous channel's compare NOW (not after the POST resolves): the
      // shown "update available" belonged to the old channel, and any check still in
      // flight for it must land in the void.
      checkGen.current++;
      setInfo(null);
    }
    setSaveErr(null);
    setSaving(true);
    try {
      setSettings(await api.setUpdateSettings(patch));
    } catch {
      setSettings(prev);
      setSaveErr("Couldn’t save update settings.");
    } finally {
      setSaving(false);
    }
  };

  const check = async () => {
    const gen = checkGen.current;
    setState("checking");
    setMsg(null);
    try {
      const result = await api.updateCheck();
      if (gen === checkGen.current) setInfo(result); // stale-channel response → dropped
      setState("idle");
    } catch {
      setState("error");
      setMsg("Couldn’t check for updates.");
    }
  };

  const apply = async () => {
    setState("applying");
    setMsg(null);
    try {
      await api.updateApply();
      setState("applied");
      setMsg("Updating… the app will restart shortly; reload in a moment.");
    } catch (e) {
      setState("error");
      setMsg(
        e instanceof ApiError && e.status === 503
          ? "Self-update isn’t available for this install."
          : e instanceof ApiError && e.status === 409
            ? "An update is already in progress."
            : "Update failed to start.",
      );
    }
  };

  const channel = settings?.channel ?? info?.channel ?? "stable";
  return (
    <section className={styles.section} aria-labelledby="updates-h">
      <h2 id="updates-h">Updates</h2>
      <dl className={styles.meta}>
        <div className={styles.metaRow}>
          <dt>Current</dt>
          <dd>{info?.current ?? current ?? "—"}</dd>
        </div>
      </dl>
      <label className={styles.aiToggle}>
        <input
          type="checkbox"
          checked={settings?.auto_update ?? false}
          disabled={!settings}
          onChange={(e) => void save({ auto_update: e.currentTarget.checked })}
        />
        <span>Automatic updates</span>
      </label>
      <p className={styles.hint}>
        Checks daily and installs new releases with the same rollback-guarded installer as
        “Update now”. No reinstall or terminal needed — the setting applies immediately.
      </p>
      {settings?.auto_update && (
        <p className={styles.hint}>
          {settings.last_auto
            ? `Last automatic check: ${new Date(settings.last_auto.ts * 1000).toLocaleString()} — ${settings.last_auto.result}`
            : "No automatic check yet since the last restart."}
        </p>
      )}
      <div className={styles.themes} role="radiogroup" aria-label="Release channel">
        {[
          { id: "stable", label: "stable", description: "Tagged releases (recommended)" },
          { id: "main", label: "main", description: "Development branch — expect rough edges" },
        ].map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={channel === o.id}
            disabled={!settings}
            className={channel === o.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard}
            onClick={() => void save({ channel: o.id })}
          >
            <span className={styles.themeName}>{o.label}</span>
            <span className={styles.themeDesc}>{o.description}</span>
          </button>
        ))}
      </div>
      <p className={styles.hint}>
        Switching back to stable waits for the next tagged release (it never downgrades on its
        own).
      </p>
      {saveErr && <p className={styles.err}>{saveErr}</p>}
      {info &&
        (info.update_available ? (
          <p className={styles.hint}>Update available: {info.latest}</p>
        ) : (
          <p className={styles.hint}>
            {info.latest ? `You’re on the latest (${info.latest}).` : "You’re up to date."}
          </p>
        ))}
      {msg && <p className={styles.hint}>{msg}</p>}
      <div className={styles.updateActions}>
        <button
          type="button"
          className={styles.updateBtn}
          onClick={check}
          disabled={state === "checking" || state === "applying" || saving}
        >
          <RefreshCw size={15} /> {state === "checking" ? "Checking…" : "Check for updates"}
        </button>
        {info?.update_available && (
          <button
            type="button"
            className={`${styles.updateApply} shine`}
            onClick={apply}
            disabled={state === "applying" || state === "applied" || saving}
          >
            <Download size={15} /> {state === "applying" ? "Updating…" : "Update now"}
          </button>
        )}
      </div>
    </section>
  );
}

/** A read-once recovery-code panel: list + copy + download. The codes live only in
 *  component state and are never persisted by the SPA (issue #116). */
function RecoveryCodes({ codes, label }: { codes: string[]; label: string }) {
  const [copied, setCopied] = useState(false);
  const text = codes.join("\n");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — the codes are visible to copy by hand */
    }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([`${text}\n`], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = "battlelab-recovery-codes.txt";
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className={styles.recoveryBox}>
      <p className={styles.warn}>{label}</p>
      <ul className={styles.recoveryList} aria-label="Recovery codes">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <div className={styles.twofaActions}>
        <button type="button" className={styles.secBtnGhost} onClick={copy}>
          <Copy size={14} /> {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className={styles.secBtnGhost} onClick={download}>
          <Download size={14} /> Download
        </button>
      </div>
    </div>
  );
}

/** A free-text proof field that resolves to a current TOTP code and/or the account
 *  password. We always send it as a password, and *also* as a code when it looks like a
 *  6-digit TOTP — so a genuine 6-digit account password can still authorize the action
 *  (the server tries the code first, then the password). */
function proofPayload(value: string): { code?: string; password?: string } {
  const v = value.trim();
  return /^\d{6}$/.test(v) ? { code: v, password: value } : { password: value };
}

/** Two-factor authentication (#116): enable (QR + manual key + confirm + recovery codes),
 *  disable, and regenerate recovery codes. Hidden when there is no login (auth_mode=none).
 *  The TOTP secret/recovery codes are shown once and never re-fetched. */
function TwoFactorCard() {
  const [authMode, setAuthMode] = useState<string | null>(null);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [enroll, setEnroll] = useState<TwoFactorEnrollment | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [confirmCode, setConfirmCode] = useState("");
  const [showDisable, setShowDisable] = useState(false);
  const [showRegen, setShowRegen] = useState(false);
  const [proof, setProof] = useState("");
  const [regenCodes, setRegenCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => {
        if (!alive) return;
        setAuthMode(c.auth_mode ?? "single-user");
        setEnabled(!!c.two_factor_enabled);
      })
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  // Client-side QR from the otpauth:// URI (bundled lib, no CDN). SVG scales to the box.
  const qrSvg = useMemo(
    () => (enroll ? encodeQR(enroll.otpauth_uri, "svg", { border: 2 }) : null),
    [enroll],
  );

  const reset = () => {
    setEnroll(null);
    setConfirmed(false);
    setConfirmCode("");
    setShowDisable(false);
    setShowRegen(false);
    setProof("");
    setRegenCodes(null);
    setError(null);
  };

  const begin = async () => {
    setBusy(true);
    setError(null);
    try {
      setEnroll(await api.enroll2fa());
      setConfirmed(false);
    } catch {
      setError("Couldn’t start enrollment.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.confirm2fa(confirmCode.trim());
      setEnabled(true);
      setConfirmed(true);
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 400
          ? "That code didn’t match — check your authenticator and try again."
          : "Couldn’t confirm the code.",
      );
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.disable2fa(proofPayload(proof));
      setEnabled(false);
      reset();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 403
          ? "Enter a current authenticator code or your password."
          : "Couldn’t disable 2FA.",
      );
    } finally {
      setBusy(false);
    }
  };

  const regenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.regenerate2fa(proofPayload(proof));
      setRegenCodes(r.recovery_codes);
      setShowRegen(false);
      setProof("");
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 403
          ? "Enter a current authenticator code or your password."
          : "Couldn’t regenerate recovery codes.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (authMode === "none") return null; // no login → 2FA is N/A

  return (
    <section className={styles.section} aria-labelledby="twofa-h">
      <h2 id="twofa-h">Two-factor authentication</h2>
      <p className={styles.hint}>
        Require a 6-digit code from an authenticator app (Google Authenticator, Authy,
        1Password, Aegis…) in addition to your password.
      </p>

      {enabled !== null && (
        <p className={styles.twofaStatus}>
          <ShieldCheck size={15} />
          <span className={`${styles.twofaBadge} ${enabled ? styles.twofaOn : styles.twofaOff}`}>
            {enabled ? "On" : "Off"}
          </span>
        </p>
      )}

      {error && <p className={styles.err}>{error}</p>}

      {/* Disabled, not mid-enrollment → offer Enable. */}
      {enabled === false && !enroll && (
        <button type="button" className={`${styles.secBtn} shine`} onClick={begin} disabled={busy}>
          <ShieldCheck size={15} /> {busy ? "Starting…" : "Enable two-factor auth"}
        </button>
      )}

      {/* Enrollment in progress: QR + manual key + recovery codes + confirm. */}
      {enroll && !confirmed && (
        <div className={styles.enrollPanel}>
          <p className={styles.hint}>1. Scan this with your authenticator app:</p>
          {qrSvg && (
            <img
              className={styles.qr}
              alt="TOTP QR code"
              src={`data:image/svg+xml,${encodeURIComponent(qrSvg)}`}
            />
          )}
          <p className={styles.hint}>…or enter this key manually:</p>
          <code className={styles.manualKey}>{enroll.secret}</code>
          <p className={styles.hint}>
            2. Save these recovery codes somewhere safe — each works once if you lose your
            device. They’re shown only now.
          </p>
          <RecoveryCodes codes={enroll.recovery_codes} label="Recovery codes (shown once)" />
          <p className={styles.hint}>3. Enter the current 6-digit code to finish:</p>
          <input
            className={styles.codeInput}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="6-digit code"
            value={confirmCode}
            onChange={(e) => setConfirmCode(e.target.value)}
          />
          <div className={styles.twofaActions}>
            <button
              type="button"
              className={styles.secBtn}
              onClick={confirm}
              disabled={busy || confirmCode.trim().length < 6}
            >
              {busy ? "Confirming…" : "Confirm & enable"}
            </button>
            <button type="button" className={styles.secBtnGhost} onClick={reset} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Just enabled: confirm the recovery codes were saved, then dismiss. */}
      {enroll && confirmed && (
        <div className={styles.enrollPanel}>
          <p className={styles.twofaStatus}>
            <ShieldCheck size={15} /> Two-factor authentication is on.
          </p>
          <RecoveryCodes
            codes={enroll.recovery_codes}
            label="Make sure you’ve saved your recovery codes — they won’t be shown again."
          />
          <button type="button" className={styles.secBtn} onClick={reset}>
            Done
          </button>
        </div>
      )}

      {/* Enabled: manage (regenerate codes / disable). */}
      {enabled === true && !enroll && (
        <div className={styles.twofaActions}>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setShowRegen((v) => !v);
              setShowDisable(false);
              setProof("");
              setError(null);
            }}
          >
            <RefreshCw size={14} /> Regenerate recovery codes
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setShowDisable((v) => !v);
              setShowRegen(false);
              setProof("");
              setError(null);
            }}
          >
            Disable
          </button>
        </div>
      )}

      {/* Fresh-proof prompt shared by disable + regenerate. */}
      {enabled === true && !enroll && (showDisable || showRegen) && (
        <div className={styles.enrollPanel}>
          <p className={styles.hint}>
            Enter a current authenticator code or your password to{" "}
            {showDisable ? "disable two-factor auth" : "regenerate your recovery codes"}.
          </p>
          <input
            className={styles.codeInput}
            type="password"
            autoComplete="off"
            placeholder="6-digit code or password"
            value={proof}
            onChange={(e) => setProof(e.target.value)}
          />
          <div className={styles.twofaActions}>
            <button
              type="button"
              className={styles.secBtn}
              onClick={showDisable ? disable : regenerate}
              disabled={busy || !proof}
            >
              {busy ? "Working…" : showDisable ? "Disable" : "Regenerate"}
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => {
                setShowDisable(false);
                setShowRegen(false);
                setProof("");
              }}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Newly regenerated codes (shown once). */}
      {regenCodes && (
        <div className={styles.enrollPanel}>
          <RecoveryCodes
            codes={regenCodes}
            label="New recovery codes — the old ones no longer work. Shown only now."
          />
          <button type="button" className={styles.secBtn} onClick={() => setRegenCodes(null)}>
            Done
          </button>
        </div>
      )}
    </section>
  );
}

/** Account (#141): a Sign out button. Hidden when there's no login (auth_mode=none), like
 *  the 2FA card. Sign out clears the session server-side, then navigates to /login. */
function AccountCard() {
  const [authMode, setAuthMode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .config()
      .then((c) => alive && setAuthMode(c.auth_mode ?? "single-user"))
      .catch(() => {
        /* unauthenticated/offline — leave it blank */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (authMode === "none") return null; // no login → nothing to sign out of

  return (
    <section className={styles.section} aria-labelledby="account-h">
      <h2 id="account-h">Account</h2>
      <p className={styles.hint}>You’re signed in to this BattleLab.</p>
      <button
        type="button"
        className={styles.secBtnGhost}
        disabled={busy}
        onClick={() => {
          setBusy(true);
          // logout hard-navigates on success; only re-enable if it threw.
          api.logout().catch(() => setBusy(false));
        }}
      >
        <LogOut size={16} /> {busy ? "Signing out…" : "Sign out"}
      </button>
    </section>
  );
}

/** Login-off explainer (#682): in Home Free / `auth_mode=none` there's no password or 2FA to
 *  manage, so the 2FA + Account cards hide and the Security tab would otherwise render empty.
 *  Show what login-off means plus the (verified) recipe to turn a password login on instead. */
function LoginOffCard() {
  return (
    <section className={styles.section} aria-labelledby="loginoff-h">
      <h2 id="loginoff-h">Login</h2>
      <p className={styles.blurb}>
        Login is off — you’re running <strong>Home Free</strong>. The app is bound to loopback and
        reached only through the blind relay with your access key, so there’s no in-app password or
        two-factor to manage here.
      </p>
      <EnableLoginDetails />
    </section>
  );
}

/** Security tab body, driven by the shared config so it never flashes the single-user cards
 *  before resolving to login-off (#682). `useConfig()` is `null` while loading — render nothing
 *  then, never assume `single-user`. */
function SecurityPanel() {
  const config = useConfig();
  if (!config) return null; // still loading — avoid a single-user flash
  if (config.auth_mode === "none") return <LoginOffCard />;
  return (
    <>
      <TwoFactorCard />
      <AccountCard />
    </>
  );
}

/** Content key over the effective discovery scope (#470). Changes exactly when the server-echoed
 *  `project_roots` / `folder_exclusions` in /api/config change, so effects that fetch the
 *  discovered folder set can depend on it without re-running for unrelated config updates. */
function useDiscoveryKey(): string {
  const config = useConfig();
  const roots = config?.project_roots ?? [];
  const exclusions = config?.folder_exclusions ?? [];
  return `${roots.join("\n")}\u0000${exclusions.join("\n")}`;
}

/** Folder discovery (#465): the operator picks the root dir(s) discovery is scoped to (a HARD
 *  scope — out-of-root folders are hidden from the sidebar too) plus a manual exclusion list for
 *  ephemerals that slip through. Empty roots ⇒ today's unscoped behaviour. Each list commits via
 *  `setPrefs`; roots/exclusions are added through the existing `~/`-rooted FolderPickerModal. */
function FolderDiscoveryCard() {
  const config = useConfig();
  // #470: a saved root/exclusion changes what /api/folders discovers — refetch /api/config so
  // the Session overview + Default project cards (keyed on the discovery prefs) refresh live.
  const refreshConfig = useConfigRefresh();
  // Optimistic local state seeded from config; reflect external changes (another device / reload)
  // via React's render-phase "adjust state on change" pattern, like the compose/default-project
  // controls. `project_roots` echoes the EFFECTIVE (normalized) list the server returns.
  const configRoots = config?.project_roots ?? [];
  const configExclusions = config?.folder_exclusions ?? [];
  const [roots, setRoots] = useState<string[]>(configRoots);
  const [exclusions, setExclusions] = useState<string[]>(configExclusions);
  const [syncedRoots, setSyncedRoots] = useState(configRoots);
  const [syncedExclusions, setSyncedExclusions] = useState(configExclusions);
  // Compare by content so a fresh array identity from config doesn't churn local edits.
  if (configRoots.join("\n") !== syncedRoots.join("\n")) {
    setSyncedRoots(configRoots);
    setRoots(configRoots);
  }
  if (configExclusions.join("\n") !== syncedExclusions.join("\n")) {
    setSyncedExclusions(configExclusions);
    setExclusions(configExclusions);
  }
  // Which picker is open ("root" | "exclusion" | null) + the trigger to refocus on close.
  const [picking, setPicking] = useState<{ kind: "root" | "exclusion"; trigger: HTMLElement | null } | null>(null);

  const commitRoots = (next: string[]) => {
    const prev = roots;
    setRoots(next);
    // The server echoes the effective (existing-dir-only) list — apply it so a non-existent pick
    // silently drops, matching what discovery will actually use.
    api
      .setPrefs({ project_roots: next })
      .then((r) => {
        const eff = (r as { project_roots?: string[] }).project_roots;
        if (Array.isArray(eff)) setRoots(eff);
        refreshConfig();
      })
      .catch(() => setRoots(prev));
  };
  const commitExclusions = (next: string[]) => {
    const prev = exclusions;
    setExclusions(next);
    api
      .setPrefs({ folder_exclusions: next })
      .then(() => refreshConfig())
      .catch(() => setExclusions(prev));
  };

  const onPick = (path: string) => {
    if (picking?.kind === "root") {
      if (!roots.includes(path)) commitRoots([...roots, path]);
    } else if (picking?.kind === "exclusion") {
      if (!exclusions.includes(path)) commitExclusions([...exclusions, path]);
    }
    setPicking(null);
  };

  return (
    <section className={styles.section} aria-labelledby="discovery-h">
      <h2 id="discovery-h">Folder discovery</h2>
      <p className={styles.hint}>
        Scope folder discovery to your project root(s). When a root is set this is a hard scope —
        folders outside it are hidden from the sidebar, filter, and pickers too. With no roots,
        discovery is unscoped (every session&rsquo;s folder plus ~/claude subdirs), as before. Add
        exclusions for scratch folders that slip through.
      </p>

      <h3 className={`${styles.aiFieldLabel} ${styles.discoverySub}`}>Root directories</h3>
      {roots.length === 0 ? (
        <p className={styles.hint}>No roots — discovery is unscoped.</p>
      ) : (
        <ul className={styles.excludeList} aria-label="Root directories">
          {roots.map((r) => (
            <li key={r} className={styles.discoveryRow}>
              <span className={styles.discoveryPath}>{shortCwd(r)}</span>
              <button
                type="button"
                className={styles.discoveryRemove}
                onClick={() => commitRoots(roots.filter((x) => x !== r))}
                aria-label={`Remove root ${shortCwd(r)}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className={styles.secBtnGhost}
        onClick={(e) => setPicking({ kind: "root", trigger: e.currentTarget })}
      >
        Add root…
      </button>

      <h3 className={`${styles.aiFieldLabel} ${styles.discoverySub}`}>Excluded folders</h3>
      {exclusions.length === 0 ? (
        <p className={styles.hint}>No exclusions.</p>
      ) : (
        <ul className={styles.excludeList} aria-label="Excluded folders">
          {exclusions.map((x) => (
            <li key={x} className={styles.discoveryRow}>
              <span className={styles.discoveryPath}>{shortCwd(x)}</span>
              <button
                type="button"
                className={styles.discoveryRemove}
                onClick={() => commitExclusions(exclusions.filter((e) => e !== x))}
                aria-label={`Remove exclusion ${shortCwd(x)}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        className={styles.secBtnGhost}
        onClick={(e) => setPicking({ kind: "exclusion", trigger: e.currentTarget })}
      >
        Add exclusion…
      </button>

      {picking && (
        <FolderPickerModal
          title={picking.kind === "root" ? "Choose a root directory" : "Choose a folder to exclude"}
          onPick={onPick}
          onCancel={() => setPicking(null)}
          returnFocusTo={picking.trigger}
        />
      )}
    </section>
  );
}

/** One project row in the Settings → Session overview card (#174). Indented by tree depth,
 *  inverse checkbox semantics, and the custom name opens a rename modal on click instead of
 *  an inline input.
 *
 *  #615: what unticking *does* depends on whether a project has adopted the folder, so the
 *  checkbox label says which. `projects_hidden` withholds a folder as a LAUNCH location for
 *  every row; for an UNADOPTED folder it additionally drops its sessions from the sidebar,
 *  the filter, and the map. An adopted folder's sessions are exempt server-side
 *  (`sessions.py` `_visible`) — a row must stay reachable in exactly one of the active /
 *  archived views, so hiding those is the project *archive*'s job, not this checkbox's.
 *  Pinned in both directions + both modes by `tests/test_projects.py`. */
function ProjectRow({
  cwd,
  depth,
  stale,
  hidden,
  adopted,
  currentName,
  onToggleHidden,
  onOpenRename,
}: {
  cwd: string;
  depth: number;
  stale: boolean;
  hidden: boolean;
  adopted: boolean;
  currentName: string;
  onToggleHidden: (cwd: string, hidden: boolean) => void;
  onOpenRename: (cwd: string, trigger: HTMLElement) => void;
}) {
  const displayName = currentName.trim();
  return (
    <li
      className={styles.excludeRow}
      style={{ paddingLeft: `${8 + depth * 18}px` }}
    >
      {/* Inverse: checked = visible, unchecked = hidden. Per #174 the user's mental model is
       *  "show this project? yes/no" — the previous "tick to hide" was confusing. */}
      <input
        type="checkbox"
        checked={!hidden}
        onChange={(e) => onToggleHidden(cwd, !e.target.checked)}
        aria-label={
          adopted
            ? `Offer ${shortCwd(cwd)} as a launch location`
            : `Show ${shortCwd(cwd)} in the sidebar, filter, and overview`
        }
      />
      <span className={styles.excludeMeta}>
        {/* Rename is offered only for UNADOPTED folders (#615 Phase 3). An adopted folder is
         *  grouped under — and labelled by — its PROJECT everywhere the app names a group (the
         *  sidebar, the filter, and the overview map, which reads `project_names` only for
         *  `kind === "folder"` groups in `overviewGraph.ts`), so a custom name typed on an adopted
         *  row would be stored and shown nowhere. Rather than keep a control that no-ops, THIS
         *  Settings row shows the folder's PATH as static text (no rename affordance) and points
         *  the user at the project. */}
        {adopted ? (
          <span className={styles.nameStatic} title="Named by its project — rename the project instead">
            {shortCwd(cwd)}
          </span>
        ) : (
          <>
            {/* Click anywhere on the name to open the rename modal. Path is a subtitle only when a
             *  custom name is set — otherwise it would just repeat the name. */}
            <button
              type="button"
              className={styles.nameButton}
              onClick={(e) => onOpenRename(cwd, e.currentTarget)}
              aria-label={`Rename ${shortCwd(cwd)}`}
            >
              {displayName || shortCwd(cwd)}
            </button>
            {displayName && <span className={styles.excludePath}>{shortCwd(cwd)}</span>}
          </>
        )}
      </span>
      {stale && <span className={styles.excludeStale}>not currently active</span>}
    </li>
  );
}

/** One owning-entity group in the reworked Session overview (#465): an entity header (color dot +
 *  name + folder count) over that entity's discovered folders, each a `ProjectRow` (inverse-checkbox
 *  visibility toggle; rename only when unadopted, #615 Phase 3), rendered as a folder sub-tree. The
 *  synthetic "Unassigned" group reuses this with a dashed dot and no entity — its folders are the
 *  renamable ones. */
function OverviewGroup({
  name,
  color,
  rows,
  adopted,
  isVisible,
  projectNames,
  onToggleHidden,
  onOpenRename,
}: {
  name: string;
  color?: string;
  rows: { cwd: string; depth: number; stale: boolean }[];
  /** False for the synthetic "Unassigned" group — every other group IS a project entity. */
  adopted: boolean;
  isVisible: (cwd: string) => boolean;
  projectNames: Record<string, string>;
  onToggleHidden: (cwd: string, hidden: boolean) => void;
  onOpenRename: (cwd: string, trigger: HTMLElement) => void;
}) {
  return (
    <div className={styles.overviewGroup}>
      <div className={styles.overviewGroupHead}>
        <span
          className={
            color
              ? styles.overviewGroupDot
              : `${styles.overviewGroupDot} ${styles.overviewGroupDotEmpty}`
          }
          style={color ? { background: color } : undefined}
          aria-hidden="true"
        />
        <span className={styles.overviewGroupName}>{name}</span>
        <span className={styles.overviewGroupCount}>{rows.length}</span>
      </div>
      <ul className={styles.excludeList} aria-label={`Folders in ${name}`}>
        {rows.map((r) => (
          <ProjectRow
            key={r.cwd}
            cwd={r.cwd}
            depth={r.depth}
            stale={r.stale}
            hidden={!isVisible(r.cwd)}
            adopted={adopted}
            currentName={projectNames[r.cwd] ?? ""}
            onToggleHidden={onToggleHidden}
            onOpenRename={onOpenRename}
          />
        ))}
      </ul>
    </div>
  );
}

/** Session overview (#174, reworked #465): discovered launch folders grouped under their owning
 *  project entity (#361), with an "Unassigned" group for folders no entity owns. Each folder keeps
 *  its inverse-checkbox visibility toggle + rename. The all/included mode radios are preserved.
 *
 *  What hiding a folder does depends on adoption (#615). For EVERY folder it withholds the folder
 *  as a launch location (`/api/folders?visible=1` has no entity carve-out). For an UNADOPTED
 *  folder it additionally drops its sessions from the sidebar list, the project filter, and the
 *  map. An ADOPTED folder's sessions survive in the sidebar and the project filter (`sessions.py`
 *  `_visible` returns True for `kind == "project"` rows), and on the map only under `project`
 *  grouping, which mirrors that exemption via `keepsHiddenCwd` — under `folder`/`agent` grouping
 *  every cluster is cwd- or engine-keyed, so a hidden cwd hides its sessions there regardless of
 *  adoption (#424). A row must stay reachable in exactly one of the active/archived views, so
 *  hiding an adopted folder's sessions is the project ARCHIVE's job. Pinned in both directions
 *  and under both modes by
 *  `tests/test_projects.py`, and on the client by `overviewGraph.test.ts`. */
function OverviewCard() {
  const {
    hiddenProjects,
    includedProjects,
    projectsMode,
    isVisible,
    setProjectVisible,
    setProjectsMode,
    projectNames,
    setProjectName,
  } = useOverviewPrefs();
  // #470: the discovered set depends on the discovery scope. FolderDiscoveryCard refreshes
  // /api/config after a save, so keying the fetch on the EFFECTIVE (server-echoed) prefs
  // re-runs it live — and only when the scope actually changed.
  const discoveryKey = useDiscoveryKey();
  const [projects, setProjects] = useState<{ cwd: string; label: string }[] | null>(null);
  const [entities, setEntities] = useState<ProjectEntity[]>([]);
  const [renaming, setRenaming] = useState<{ cwd: string; trigger: HTMLElement | null } | null>(
    null,
  );

  useEffect(() => {
    let alive = true;
    api
      .folders()
      .then((d) => alive && setProjects(d.folders))
      .catch(() => alive && setProjects([])); // discovery failed → empty, not a dead control
    return () => {
      alive = false;
    };
  }, [discoveryKey]);

  useEffect(() => {
    let alive = true;
    // Entities drive the grouping (#465). A failed fetch → no groups, everything Unassigned.
    // Mount-only: entity ownership doesn't depend on the discovery scope.
    api
      .projectEntities()
      .then((d) => alive && setEntities(d.projects))
      .catch(() => alive && setEntities([]));
    return () => {
      alive = false;
    };
  }, []);

  // Group the discovered (∪ curated-but-inactive ∪ named) folders by owning entity (#465). A
  // curated-but-inactive project (hidden in `all` mode, or included-but-not-currently-discovered
  // in `included` mode) and a rename for an inactive project all stay editable here. Within a
  // group, folders are still rendered as a nesting tree (buildProjectTree/flattenTree), so an
  // adopted parent/child pair indents the same way as before.
  const groups = useMemo(() => {
    const known = new Set((projects ?? []).map((p) => p.cwd));
    const all = new Set<string>([
      ...known,
      ...hiddenProjects,
      ...includedProjects,
      ...Object.keys(projectNames),
    ]);
    // Owner id → its cwds. "" is the Unassigned bucket.
    const byOwner = new Map<string, Set<string>>();
    for (const cwd of all) {
      const owner = owningProjectId(cwd, entities);
      const bucket = byOwner.get(owner) ?? new Set<string>();
      bucket.add(cwd);
      byOwner.set(owner, bucket);
    }
    const toRows = (cwds: Set<string>) =>
      flattenTree(buildProjectTree(cwds)).map((n) => ({
        cwd: n.cwd,
        depth: n.depth,
        stale: !known.has(n.cwd),
      }));
    // One group per entity that owns ≥1 discovered folder, entities first (by name), then
    // Unassigned last.
    const entityGroups = entities
      .filter((e) => (byOwner.get(e.id)?.size ?? 0) > 0)
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((e) => ({
        key: e.id,
        name: e.name,
        color: e.color || undefined,
        rows: toRows(byOwner.get(e.id)!),
      }));
    const unassigned = byOwner.get("");
    if (unassigned && unassigned.size > 0) {
      entityGroups.push({
        key: "__unassigned__",
        name: "Unassigned",
        color: undefined,
        rows: toRows(unassigned),
      });
    }
    return entityGroups;
  }, [projects, entities, hiddenProjects, includedProjects, projectNames]);

  const total = useMemo(() => groups.reduce((n, g) => n + g.rows.length, 0), [groups]);
  const curated = projectsMode === "included";
  return (
    <section className={styles.section} aria-labelledby="overview-h">
      <h2 id="overview-h">Session overview</h2>
      {/* Visibility mode (#335). "Show all" = the legacy denylist (untick to hide). "Only included"
       *  = a curated allowlist: only ticked projects show, and a new directory never auto-appears
       *  until you tick it (starting a session in a directory also adds it automatically). */}
      <div className={styles.modeRow} role="radiogroup" aria-label="Project visibility">
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="projects-mode"
            checked={!curated}
            onChange={() => setProjectsMode("all")}
          />
          Show all (hide a few)
        </label>
        <label className={styles.modeOpt}>
          <input
            type="radio"
            name="projects-mode"
            checked={curated}
            onChange={() => setProjectsMode("included")}
          />
          Only included
        </label>
      </div>
      {/* #615: state what unticking actually does, per row kind. Unticking always withholds a
       *  folder as a launch location; only for an UNADOPTED folder does it also drop its
       *  sessions from the sidebar/filter/map. A project's sessions stay visible either way —
       *  archive the project to hide those. The old copy promised "hide it everywhere", which
       *  was never true for adopted folders. */}
      <p className={styles.hint}>
        {curated
          ? "Folders are grouped under their owning project. Only ticked folders are offered as launch locations; an unticked, unassigned folder also drops out of the sidebar, filter, and overview map. New directories stay hidden until you tick them (starting a session in one adds it automatically)."
          : "Folders are grouped under their owning project. Unticking a folder stops it being offered as a launch location; if no project has adopted it, its sessions also disappear from the sidebar, filter, and overview map. Filtering still uses the full path under the hood."}
      </p>
      <p className={styles.hint}>
        A project&rsquo;s sessions stay in the sidebar and filter even with its folders unticked —
        archive the project to hide those. Click an <em>unassigned</em> folder&rsquo;s name to give
        it a custom display name; an adopted folder takes its label from its project, so rename the
        project (above) instead.
      </p>
      {projects === null ? (
        <p className={styles.hint}>Loading projects…</p>
      ) : total === 0 ? (
        <p className={styles.hint}>No projects discovered yet.</p>
      ) : (
        // Reuse ProjectRow's inverse-checkbox: `hidden` = NOT visible under the current mode; a
        // toggle routes through `setProjectVisible`, which writes the allowlist (included) or the
        // denylist (all) — never both (#335).
        groups.map((g) => (
          <OverviewGroup
            key={g.key}
            name={g.name}
            color={g.color}
            rows={g.rows}
            adopted={g.key !== "__unassigned__"}
            isVisible={isVisible}
            projectNames={projectNames}
            onToggleHidden={(cwd, hidden) => setProjectVisible(cwd, !hidden)}
            onOpenRename={(cwd, trigger) => setRenaming({ cwd, trigger })}
          />
        ))
      )}
      {renaming && (
        <RenameProjectModal
          cwd={renaming.cwd}
          initialName={projectNames[renaming.cwd] ?? ""}
          onCancel={() => setRenaming(null)}
          onSave={(name) => {
            setProjectName(renaming.cwd, name);
            setRenaming(null);
          }}
          returnFocusTo={renaming.trigger}
        />
      )}
    </section>
  );
}

/** Maintenance (#142): bulk-archive sessions older than N hours. Reversible (archived
 *  sessions can be unarchived); a two-step confirm guards the bulk action. */
function CleanupCard() {
  const [hours, setHours] = useState(168); // default: a week
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.archiveOlder(hours);
      setResult(
        `Archived ${r.archived} session${r.archived === 1 ? "" : "s"}` +
          (r.skipped ? ` (${r.skipped} skipped).` : "."),
      );
    } catch {
      setResult("Couldn’t archive — please try again.");
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const valid = Number.isFinite(hours) && hours > 0;

  return (
    <section className={styles.section} aria-labelledby="cleanup-h">
      <h2 id="cleanup-h">Maintenance</h2>
      <p className={styles.hint}>
        Archive sessions you haven’t touched in a while. Archived sessions are hidden from the
        list but can be unarchived — nothing is deleted.
      </p>
      <div className={styles.cleanupRow}>
        <label className={styles.cleanupLabel}>
          Older than
          <input
            className={styles.hoursInput}
            type="number"
            min={1}
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            aria-label="Age in hours"
          />
          hours
        </label>
        {confirming ? (
          <span className={styles.confirmRow}>
            <button type="button" className={styles.danger} disabled={busy} onClick={run}>
              <Archive size={16} /> {busy ? "Archiving…" : "Confirm archive"}
            </button>
            <button
              type="button"
              className={styles.secBtnGhost}
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className={styles.secBtnGhost}
            disabled={!valid}
            onClick={() => {
              setResult(null);
              setConfirming(true);
            }}
          >
            <Archive size={16} /> Archive older
          </button>
        )}
      </div>
      {result && <p className={styles.hint}>{result}</p>}
    </section>
  );
}

/** Scrollback cache (#206): the per-session terminal-history files that make scrollback
 *  survive restarts. Shows the cache size and lets the user reclaim it — either just the
 *  archived sessions' caches, or everything. A two-step confirm guards each clear. */
function ScrollbackCacheCard() {
  const [info, setInfo] = useState<{ bytes: number; files: number } | null>(null);
  const [confirming, setConfirming] = useState<"all" | "archived" | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const refresh = () =>
    api
      .scrollbackInfo()
      .then(setInfo)
      .catch(() => setInfo(null));

  useEffect(() => {
    void refresh();
  }, []);

  const clear = async (scope: "all" | "archived") => {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.clearScrollback(scope);
      setResult(
        `Cleared ${r.removed} cache file${r.removed === 1 ? "" : "s"} (${humanBytes(r.bytes_freed)} freed).`,
      );
      await refresh();
    } catch {
      setResult("Couldn’t clear the cache — please try again.");
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  return (
    <section className={styles.section} aria-labelledby="scrollback-h">
      <h2 id="scrollback-h">Scrollback cache</h2>
      <p className={styles.hint}>
        Terminal history is cached on disk per session so scrollback survives restarts.
        {info ? ` Currently ${humanBytes(info.bytes)} across ${info.files} session${info.files === 1 ? "" : "s"}.` : ""}{" "}
        Clearing only drops cached scrollback — sessions and their on-disk transcripts are
        untouched.
      </p>
      {confirming ? (
        <span className={styles.confirmRow}>
          <button
            type="button"
            className={styles.danger}
            disabled={busy}
            onClick={() => void clear(confirming)}
          >
            <Trash2 size={16} />{" "}
            {busy
              ? "Clearing…"
              : confirming === "all"
                ? "Confirm clear all"
                : "Confirm clear archived"}
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => setConfirming(null)}
            disabled={busy}
          >
            Cancel
          </button>
        </span>
      ) : (
        <div className={styles.cleanupRow}>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setResult(null);
              setConfirming("archived");
            }}
          >
            <Archive size={16} /> Clear archived sessions’ cache
          </button>
          <button
            type="button"
            className={styles.secBtnGhost}
            onClick={() => {
              setResult(null);
              setConfirming("all");
            }}
          >
            <Trash2 size={16} /> Clear all cache
          </button>
        </div>
      )}
      {result && <p className={styles.hint}>{result}</p>}
    </section>
  );
}

/** Settings (#109, tabbed in #357): a keyboard-accessible tab shell over the existing
 *  sections — Appearance, Projects, AI Review (#356 placeholder), Security, System,
 *  Maintenance, About. Deep-linkable as /settings/:tab. Reached via the gear in the topbar. */
export function Settings() {
  const { theme, setTheme } = useTheme();
  const { accent, setAccent } = useAccent();
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
  // Compose default (#254): persisted via /api/prefs; applies to sessions opened after the
  // next config load. Seed from the loaded config and reflect external changes (other device).
  const configCompose = useConfig()?.compose_default ?? "auto";
  const [composeMode, setComposeMode] = useState<string>(configCompose);
  const [syncedCompose, setSyncedCompose] = useState(configCompose);
  if (configCompose !== syncedCompose) {
    setSyncedCompose(configCompose);
    setComposeMode(configCompose);
  }
  const chooseCompose = (mode: string) => {
    const prev = composeMode;
    setComposeMode(mode);
    api.setPrefs({ compose_default: mode }).catch(() => setComposeMode(prev));
  };
  // Session list order (#506): persisted via /api/prefs; the server sorts the list. Optimistic
  // with rollback, like the others. A successful save refreshes the shared config (#548): the
  // sidebar list watches the config's order and re-sorts in place — no waiting for the poll.
  const configOrder = useConfig()?.session_list_order ?? "recent_activity";
  const refreshConfig = useConfigRefresh();
  const [listOrder, setListOrder] = useState<string>(configOrder);
  const [syncedOrder, setSyncedOrder] = useState(configOrder);
  if (configOrder !== syncedOrder) {
    setSyncedOrder(configOrder);
    setListOrder(configOrder);
  }
  const chooseOrder = (mode: string) => {
    const prev = listOrder;
    setListOrder(mode);
    api
      .setPrefs({ session_list_order: mode })
      .then(() => refreshConfig())
      .catch(() => setListOrder(prev));
  };
  const commitHex = () => {
    const norm = normalizeAccent(hexDraft);
    if (norm) setAccent(norm);
    else setHexDraft(accent); // reset an invalid entry back to the active accent
  };
  const [version, setVersion] = useState<string | null>(null);
  // Return to wherever the gear was tapped from (#155) — the session, overview, or landing —
  // instead of always dropping to the new-session landing. Only trust an in-app path, and
  // never Settings itself (any tab URL) — no loop (#357).
  const location = useLocation();
  const returnTo = (() => {
    const r = (location.state as { returnTo?: unknown } | null)?.returnTo;
    return typeof r === "string" &&
      r.startsWith("/") &&
      !r.startsWith("//") &&
      r !== "/settings" &&
      !r.startsWith("/settings/")
      ? r
      : "/";
  })();
  // Canonical tab from the URL (#357): /settings/:tab. Bare /settings and unknown tabs
  // both land on the first tab via a replace-redirect (state rides along so the #155
  // back link survives the hop).
  const { tab } = useParams<{ tab: string }>();

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

  if (!isSettingsTab(tab)) {
    return <Navigate to={`/settings/${DEFAULT_SETTINGS_TAB}`} replace state={location.state} />;
  }

  return (
    <div className={styles.wrap}>
      <header className={styles.head}>
        <Link to={returnTo} className={styles.back} aria-label="Back to sessions">
          <ArrowLeft size={18} />
        </Link>
        <h1>Settings</h1>
      </header>

      <SettingsTablist active={tab} />

      <div
        role="tabpanel"
        id={`settings-panel-${tab}`}
        aria-labelledby={`settings-tab-${tab}`}
        className={styles.panel}
      >
        {tab === "appearance" && (
          <>
            <section className={styles.section} aria-labelledby="appearance-h">
              <h2 id="appearance-h">Appearance</h2>
              <p className={styles.hint}>Choose how BattleLab looks.</p>
              <div className={styles.themes} role="radiogroup" aria-label="Theme">
                {THEME_LIST.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    role="radio"
                    aria-checked={theme === t.id}
                    className={
                      theme === t.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard
                    }
                    onClick={() => setTheme(t.id)}
                  >
                    <span
                      className={`${styles.swatch} ${styles[`sw_${t.id}`]}`}
                      aria-hidden="true"
                    />
                    <span className={styles.themeName}>{t.label}</span>
                    <span className={styles.themeDesc}>{t.description}</span>
                  </button>
                ))}
              </div>

              <h3 className={styles.subhead} id="accent-h">
                Accent
              </h3>
              <p className={styles.hint}>
                The brand colour — buttons, highlights, the terminal cursor.
              </p>
              <div className={styles.accents} role="radiogroup" aria-labelledby="accent-h">
                {ACCENT_PRESETS.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    role="radio"
                    aria-checked={accent === p.hex}
                    aria-label={p.label}
                    title={p.label}
                    className={
                      accent === p.hex ? `${styles.accentDot} ${styles.active}` : styles.accentDot
                    }
                    style={{ "--dot": p.hex } as CSSProperties}
                    onClick={() => setAccent(p.hex)}
                  />
                ))}
                <label className={styles.accentCustom} title="Custom colour">
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
                  className={styles.accentHex}
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
              </div>

              <h3 className={styles.subhead} id="compose-h">
                Compose box
              </h3>
              <p className={styles.hint}>
                Default state when a session opens. Applies after the next reload.
              </p>
              <div className={styles.themes} role="radiogroup" aria-labelledby="compose-h">
                {[
                  { id: "auto", label: "Auto", description: "Open on touch, collapsed on desktop" },
                  { id: "open", label: "Open", description: "Always expanded on load" },
                  { id: "collapsed", label: "Collapsed", description: "Always collapsed to the bar" },
                ].map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    role="radio"
                    aria-checked={composeMode === o.id}
                    className={
                      composeMode === o.id
                        ? `${styles.themeCard} ${styles.active}`
                        : styles.themeCard
                    }
                    onClick={() => chooseCompose(o.id)}
                  >
                    <span className={styles.themeName}>{o.label}</span>
                    <span className={styles.themeDesc}>{o.description}</span>
                  </button>
                ))}
              </div>

              <h3 className={styles.subhead} id="listorder-h">
                Session list order
              </h3>
              <p className={styles.hint}>
                How sessions are sorted in the sidebar. Favorites always pin to the top.
              </p>
              <div className={styles.themes} role="radiogroup" aria-labelledby="listorder-h">
                {[
                  {
                    id: "recent_activity",
                    label: "Recent activity",
                    description: "Newest update first (default)",
                  },
                  {
                    id: "created_at",
                    label: "Creation date",
                    description: "Newest-created first; order stays put as sessions update",
                  },
                ].map((o) => (
                  <button
                    key={o.id}
                    type="button"
                    role="radio"
                    aria-checked={listOrder === o.id}
                    className={
                      listOrder === o.id
                        ? `${styles.themeCard} ${styles.active}`
                        : styles.themeCard
                    }
                    onClick={() => chooseOrder(o.id)}
                  >
                    <span className={styles.themeName}>{o.label}</span>
                    <span className={styles.themeDesc}>{o.description}</span>
                  </button>
                ))}
              </div>
            </section>
          </>
        )}

        {tab === "projects" && (
          <>
            {/* Entities first (#361 Phase 3): what sessions BELONG to. The folder
                visibility/rename cards below stay about where sessions LAUNCH. */}
            <ProjectsManagerCard />
            {/* Folder discovery scope + exclusions (#465), above the (now entity-grouped) overview. */}
            <FolderDiscoveryCard />
            <OverviewCard />
          </>
        )}

        {tab === "ai-review" && (
          <>
            <AiActivityPanel />
            <AiReviewSettings />
            <AutoSortSettings />
            <PulseSettings />
          </>
        )}

        {tab === "security" && <SecurityPanel />}

        {tab === "system" && (
          <>
            <ConnectedAgents />
            <SystemCard />
            <UpdatesCard />
          </>
        )}

        {tab === "maintenance" && (
          <>
            <CleanupCard />
            <ScrollbackCacheCard />
          </>
        )}

        {tab === "about" && (
          <>
            <section className={styles.section} aria-labelledby="support-h">
              <h2 id="support-h">Support</h2>
              <p className={styles.blurb}>
                If BattleLab saves you time, you can support its development.
              </p>
              <a
                className={`${styles.coffee} shine`}
                href={BUY_ME_A_COFFEE}
                target="_blank"
                rel="noopener noreferrer"
              >
                <Coffee size={16} /> Buy me a coffee
              </a>
            </section>

            <section className={styles.section} aria-labelledby="about-h">
              <h2 id="about-h">About</h2>
              <p className={styles.brandLine}>
                Battle<b>Lab</b>
              </p>
              <p className={styles.hint}>Command &amp; Code</p>
              <p className={styles.blurb}>
                The mobile-first organizer for your AI-coding sessions — claude, opencode, codex,
                gemini, antigravity and kimi, all in one place.
              </p>
              <dl className={styles.meta}>
                <dt>Version</dt>
                <dd>{version ?? "…"}</dd>
                <dt>Created by</dt>
                <dd>
                  <a
                    className={styles.nameLink}
                    href="https://superstatus.io"
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Marcus Braun
                  </a>
                </dd>
              </dl>
              <div className={styles.aboutLinks}>
                <a
                  className={styles.aboutLink}
                  href={SOURCE_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                >
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
          </>
        )}
      </div>
    </div>
  );
}
