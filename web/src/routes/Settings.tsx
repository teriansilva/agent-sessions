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
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfig } from "../app/config";
import { useOverviewPrefs } from "../app/overviewPrefs";
import { api, ApiError } from "../lib/api";
import { engineName, humanBytes, humanDuration, shortCwd } from "../lib/format";
import { buildProjectTree, flattenTree } from "../lib/projectTree";
import { RenameProjectModal } from "./RenameProjectModal";
import { ACCENT_PRESETS, normalizeAccent } from "../theme/accent";
import { useAccent } from "../theme/accentStore";
import { THEME_LIST } from "../theme/themes";
import { useTheme } from "../theme/themeStore";
import type { EngineInfo, SystemInfo, TwoFactorEnrollment, UpdateInfo } from "../types/api";
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
 *  yet, the check reports "up to date" (no `latest`). */
function UpdatesCard() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "checking" | "applying" | "applied" | "error">(
    "idle",
  );
  const [msg, setMsg] = useState<string | null>(null);

  // Show the running version immediately; the remote compare (a git ls-remote) only runs
  // when the user clicks "Check for updates".
  useEffect(() => {
    let alive = true;
    api
      .version()
      .then((v) => alive && setCurrent(v.version))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const check = async () => {
    setState("checking");
    setMsg(null);
    try {
      setInfo(await api.updateCheck());
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
          : "Update failed to start.",
      );
    }
  };

  return (
    <section className={styles.section} aria-labelledby="updates-h">
      <h2 id="updates-h">Updates</h2>
      <dl className={styles.meta}>
        <div className={styles.metaRow}>
          <dt>Current</dt>
          <dd>{info?.current ?? current ?? "—"}</dd>
        </div>
        <div className={styles.metaRow}>
          <dt>Channel</dt>
          <dd>{info?.channel ?? "stable"}</dd>
        </div>
      </dl>
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
          disabled={state === "checking" || state === "applying"}
        >
          <RefreshCw size={15} /> {state === "checking" ? "Checking…" : "Check for updates"}
        </button>
        {info?.update_available && (
          <button
            type="button"
            className={`${styles.updateApply} shine`}
            onClick={apply}
            disabled={state === "applying" || state === "applied"}
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

/** One project row in the Settings → Session overview card (#174). Indented by tree depth,
 *  inverse checkbox semantics (checked = visible; unchecked = hidden everywhere), and the
 *  custom name opens a rename modal on click instead of an inline input. */
function ProjectRow({
  cwd,
  depth,
  stale,
  hidden,
  currentName,
  onToggleHidden,
  onOpenRename,
}: {
  cwd: string;
  depth: number;
  stale: boolean;
  hidden: boolean;
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
        aria-label={`Show ${shortCwd(cwd)} in the sidebar, filter, and overview`}
      />
      <span className={styles.excludeMeta}>
        {/* Click anywhere on the name to open the rename modal. Path is shown as a subtitle
         *  only when a custom name is set — otherwise it would just repeat the name. */}
        <button
          type="button"
          className={styles.nameButton}
          onClick={(e) => onOpenRename(cwd, e.currentTarget)}
          aria-label={`Rename ${shortCwd(cwd)}`}
        >
          {displayName || shortCwd(cwd)}
        </button>
        {displayName && <span className={styles.excludePath}>{shortCwd(cwd)}</span>}
      </span>
      {stale && <span className={styles.excludeStale}>not currently active</span>}
    </li>
  );
}

/** Session overview (#174): hierarchical project tree with inverse-checkbox + rename modal.
 *  Hide is GLOBAL — affects sidebar list, project filter, new-session picker, and the map. */
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
  const [projects, setProjects] = useState<{ cwd: string; label: string }[] | null>(null);
  const [renaming, setRenaming] = useState<{ cwd: string; trigger: HTMLElement | null } | null>(
    null,
  );

  useEffect(() => {
    let alive = true;
    api
      .projects()
      .then((d) => alive && setProjects(d.projects))
      .catch(() => alive && setProjects([])); // discovery failed → empty, not a dead control
    return () => {
      alive = false;
    };
  }, []);

  // Union of: known (discovered) cwds ∪ hidden ∪ included ∪ named — so a curated-but-inactive
  // project (hidden in `all` mode, or included-but-not-currently-discovered in `included` mode)
  // and a rename for an inactive project all stay editable here.
  const rows = useMemo(() => {
    const known = new Set((projects ?? []).map((p) => p.cwd));
    const all = new Set<string>([
      ...known,
      ...hiddenProjects,
      ...includedProjects,
      ...Object.keys(projectNames),
    ]);
    const tree = buildProjectTree(all);
    return flattenTree(tree).map((n) => ({
      cwd: n.cwd,
      depth: n.depth,
      stale: !known.has(n.cwd),
    }));
  }, [projects, hiddenProjects, includedProjects, projectNames]);

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
      <p className={styles.hint}>
        {curated
          ? "Only ticked projects show — sidebar, filter, and overview map. New directories stay hidden until you tick them (starting a session in one adds it automatically). Click a name for a custom display name."
          : "Untick a project to hide it everywhere — sidebar, filter, new-session picker, and the overview map. Click a name to give the project a custom display name. Filtering still uses the full path under the hood."}
      </p>
      {projects === null ? (
        <p className={styles.hint}>Loading projects…</p>
      ) : rows.length === 0 ? (
        <p className={styles.hint}>No projects discovered yet.</p>
      ) : (
        <ul className={styles.excludeList} aria-label="Projects">
          {rows.map((r) => (
            <ProjectRow
              key={r.cwd}
              cwd={r.cwd}
              depth={r.depth}
              stale={r.stale}
              // Reuse ProjectRow's inverse-checkbox: `hidden` = NOT visible under the current mode;
              // a toggle routes through `setProjectVisible`, which writes the allowlist (included)
              // or the denylist (all) — never both (#335).
              hidden={!isVisible(r.cwd)}
              currentName={projectNames[r.cwd] ?? ""}
              onToggleHidden={(cwd, hidden) => setProjectVisible(cwd, !hidden)}
              onOpenRename={(cwd, trigger) => setRenaming({ cwd, trigger })}
            />
          ))}
        </ul>
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

/** Settings (#109): theme picker (applies app-wide + to the terminal), an About section
 *  with the running version, and a support link. Reached via the gear in the sidebar. */
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
  // Experimental: VT scrollback (#329) — faithful real-frame scroll-up on switch. Persisted via
  // /api/prefs; the server flips it live (best-effort starts the sidecar). Optimistic with rollback.
  const configVt = useConfig()?.vt_scrollback ?? false;
  const [vtScrollback, setVtScrollback] = useState<boolean>(configVt);
  const [syncedVt, setSyncedVt] = useState(configVt);
  if (configVt !== syncedVt) {
    setSyncedVt(configVt);
    setVtScrollback(configVt);
  }
  const toggleVt = (on: boolean) => {
    const prev = vtScrollback;
    setVtScrollback(on);
    api.setPrefs({ vt_scrollback: on }).catch(() => setVtScrollback(prev));
  };
  const commitHex = () => {
    const norm = normalizeAccent(hexDraft);
    if (norm) setAccent(norm);
    else setHexDraft(accent); // reset an invalid entry back to the active accent
  };
  const [version, setVersion] = useState<string | null>(null);
  // Return to wherever the gear was tapped from (#155) — the session, overview, or landing —
  // instead of always dropping to the new-session landing. Only trust an in-app path.
  const location = useLocation();
  const returnTo = (() => {
    const r = (location.state as { returnTo?: unknown } | null)?.returnTo;
    return typeof r === "string" && r.startsWith("/") && !r.startsWith("//") && r !== "/settings"
      ? r
      : "/";
  })();

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
        <Link to={returnTo} className={styles.back} aria-label="Back to sessions">
          <ArrowLeft size={18} />
        </Link>
        <h1>Settings</h1>
      </header>

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
              className={theme === t.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard}
              onClick={() => setTheme(t.id)}
            >
              <span className={`${styles.swatch} ${styles[`sw_${t.id}`]}`} aria-hidden="true" />
              <span className={styles.themeName}>{t.label}</span>
              <span className={styles.themeDesc}>{t.description}</span>
            </button>
          ))}
        </div>

        <h3 className={styles.subhead} id="accent-h">
          Accent
        </h3>
        <p className={styles.hint}>The brand colour — buttons, highlights, the terminal cursor.</p>
        <div className={styles.accents} role="radiogroup" aria-labelledby="accent-h">
          {ACCENT_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              role="radio"
              aria-checked={accent === p.hex}
              aria-label={p.label}
              title={p.label}
              className={accent === p.hex ? `${styles.accentDot} ${styles.active}` : styles.accentDot}
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
                composeMode === o.id ? `${styles.themeCard} ${styles.active}` : styles.themeCard
              }
              onClick={() => chooseCompose(o.id)}
            >
              <span className={styles.themeName}>{o.label}</span>
              <span className={styles.themeDesc}>{o.description}</span>
            </button>
          ))}
        </div>
      </section>

      <section className={styles.section} aria-labelledby="experimental-h">
        <h2 id="experimental-h">Experimental</h2>
        <h3 className={styles.subhead}>Faithful scroll-up (VT)</h3>
        <p className={styles.hint}>
          Seeds the terminal with the agent&rsquo;s real current frame when you switch sessions
          (via the VT sidecar) instead of relying on a repaint nudge &mdash; more accurate, but
          experimental and can rarely garble. Takes effect on the next session switch; turn it off
          if you see issues.
        </p>
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={vtScrollback}
            onChange={(e) => toggleVt(e.currentTarget.checked)}
          />
          <span>{vtScrollback ? "Enabled" : "Disabled"}</span>
        </label>
      </section>

      <OverviewCard />

      <TwoFactorCard />

      <AccountCard />

      <ConnectedAgents />

      <SystemCard />

      <UpdatesCard />

      <CleanupCard />

      <ScrollbackCacheCard />

      <section className={styles.section} aria-labelledby="support-h">
        <h2 id="support-h">Support</h2>
        <p className={styles.blurb}>If BattleLab saves you time, you can support its development.</p>
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
          The mobile-first organizer for your AI-coding sessions — claude, opencode, codex and
          gemini, all in one place.
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
    </div>
  );
}
