/** Library → MCP servers → Add (#2163 P1c): the five-step setup on the app's one wizard shell —
 *  Source · Review · Settings · Check · Enable.
 *
 *  - **Source** registers the server (nothing is fetched). Git is shown, and not pickable yet.
 *  - **Review** shows what will run. A remote server has nothing to install; a package is resolved
 *    into a lock (`/review`) and installed only when the operator presses Install (`/install`).
 *    Both are operations the page polls (`useMcpOperation`): a package can take minutes.
 *  - **Settings** stores each NEW credential in Variables (write-only, one request), then saves the
 *    rows naming it, and runs the shared check.
 *  - **Check** shows each step's LED and evidence; a failure offers Edit credentials / Run again.
 *  - **Enable** turns the server on per project. A project without its own credentials gets the
 *    shared ones bound (Variables' explicit reference); one whose set has no passing check is
 *    checked first and stays off if that fails. */
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Box, GitBranch, Globe, KeyRound, Package, Power } from "lucide-react";

import { api } from "../lib/api";
import { MCP_PATH, mcpServerPath } from "../lib/routes";
import type { McpCheckRow, McpReviewFacts, McpServerDetail, McpServerSummary, McpSource } from "../types/mcp";
import { WizardShell, type WizardStep } from "../components/wizard/WizardShell";
import { Callsign, Panel } from "../components/hud/Panel";
import { Field, Seg } from "../components/hud/Controls";
import { Loader } from "../components/hud/Loader";
import {
  Caution,
  CheckReport,
  ProjectSquare,
  RemoteFacts,
  ReviewFacts,
  RunAgainLabel,
  StateWords,
  WhatHappensNext,
} from "../components/mcp/McpBits";
import { SettingsEditor } from "../components/mcp/SettingsEditor";
import { useMcpOperation } from "../components/mcp/mcpOperation";
import { bindShared, errorText, storeNewCredentials, useProjects, useSecrets } from "../components/mcp/mcpData";
import {
  argsFromText,
  bindPlan,
  checkView,
  credentialWords,
  enableAction,
  finalProjects,
  nameError,
  rowsFromSettings,
  settingsFromRows,
  sharedAvailable,
  suggestName,
  validateRows,
  verdict,
  type EnableAction,
  type RowErrors,
  type SettingRow,
} from "../components/mcp/mcpModel";
import styles from "../components/mcp/mcp.module.css";

type Kind = "remote" | "npm" | "pypi";

const STEPS: WizardStep[] = [
  { id: "source", label: "Source" },
  { id: "review", label: "Review" },
  { id: "settings", label: "Settings" },
  { id: "check", label: "Check" },
  { id: "enable", label: "Enable" },
];
const HEADINGS = [
  "Where the server comes from",
  "What will run",
  "Settings & credentials",
  "Check that it works",
  "Enable for projects",
];

const sameSource = (a: McpSource | undefined, b: McpSource | undefined) => JSON.stringify(a) === JSON.stringify(b);

export default function McpWizard() {
  const navigate = useNavigate();
  const { projects, name: projectName, color } = useProjects();
  const secretsState = useSecrets();
  const [step, setStep] = useState(0);
  const [server, setServer] = useState<McpServerSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // ---- step 1: source
  const [kind, setKind] = useState<Kind>("remote");
  const [url, setUrl] = useState("");
  const [pkg, setPkg] = useState("");
  const [version, setVersion] = useState("");
  const [entrypoint, setEntrypoint] = useState("");
  const [nameInput, setNameInput] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [label, setLabel] = useState("");
  const [tried, setTried] = useState(false);

  const source: McpSource = useMemo(
    () =>
      kind === "remote"
        ? { kind, url: url.trim() }
        : {
            kind,
            package: pkg.trim(),
            version: version.trim(),
            ...(kind === "pypi" && entrypoint.trim() ? { entrypoint: entrypoint.trim() } : {}),
          },
    [kind, url, pkg, version, entrypoint],
  );
  // The name follows the source until the operator types one of their own.
  const name = nameTouched ? nameInput : suggestName(source);

  const sourceErrors = {
    url: kind === "remote" && !/^https:\/\/\S+$/.test(url.trim()) ? "An https:// address." : "",
    pkg: kind !== "remote" && !pkg.trim() ? "The package's name on the registry." : "",
    version:
      kind !== "remote" && !/^\d+(\.\d+)*([-.+][0-9A-Za-z.-]+)?$/.test(version.trim())
        ? "An exact version, e.g. 1.4.2. Ranges and ‘latest’ are refused."
        : "",
    name: nameError(name),
  };
  const sourceValid = !Object.values(sourceErrors).some(Boolean);

  const saveSource = async () => {
    setTried(true);
    if (!sourceValid) return;
    setBusy(true);
    setError("");
    try {
      let next: McpServerSummary;
      if (!server) {
        next = await api.createMcpServer({ name, label: label.trim(), source });
      } else if ((server.source.kind === "remote") !== (kind === "remote")) {
        // A server keeps its kind; this one was only just registered here, so start it again.
        await api.deleteMcpServer(server.id, server.updated_at);
        next = await api.createMcpServer({ name, label: label.trim(), source });
      } else if (server.name !== name || server.label !== label.trim() || !sameSource(server.source, source)) {
        next = await api.updateMcpServer(server.id, { name, label: label.trim(), source }, server.updated_at);
      } else next = server;
      const changed = !sameSource(next.source, server?.source);
      if (changed) setFacts(null);
      setServer(next);
      setStep(1);
      const nextInstalled =
        !!next.installed && sameSource((next.installed as { source?: McpSource }).source, next.source);
      if (next.transport === "stdio" && !nextInstalled && (changed || (!facts && !reviewError)))
        void runReview(next);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // ---- step 2: review
  const [facts, setFacts] = useState<McpReviewFacts | null>(null);
  const [reviewError, setReviewError] = useState("");
  const isPackage = server?.transport === "stdio";
  const installed = !!server?.installed && sameSource((server.installed as { source?: McpSource }).source, server.source);
  // Both run on the server as polled operations (#2163 P1b): a package can take minutes.
  const reviewOp = useMcpOperation();
  const installOp = useMcpOperation();
  const runReview = async (target: McpServerSummary | null = server) => {
    if (!target) return;
    setReviewError("");
    setFacts(null);
    try {
      const reviewed = await reviewOp(`${target.id}@${target.updated_at}`, (rid) => api.reviewMcpServer(target.id, rid));
      if (reviewed) setFacts(reviewed);
    } catch (e) {
      setReviewError(errorText(e));
    }
  };
  const install = async () => {
    if (!server || !facts) return;
    setBusy(true);
    setError("");
    try {
      const next = await installOp(`${server.id}:${facts.review_id}`, (rid) =>
        api.installMcpServer(server.id, facts.review_id, rid),
      );
      if (!next) return;
      setServer(next);
      setStep(2);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // ---- step 3: settings
  const [rows, setRows] = useState<SettingRow[]>([]);
  const [argsText, setArgsText] = useState("");
  const [rowErrors, setRowErrors] = useState<Record<string, RowErrors>>({});
  const [dirty, setDirty] = useState(false);
  const editRows = (next: SettingRow[]) => {
    setRows(next);
    setDirty(true);
  };
  const saveSettings = async () => {
    if (!server) return;
    const errs = validateRows(rows, server.transport, secretsState.names);
    setRowErrors(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    setError("");
    try {
      const stored = await storeNewCredentials(rows);
      setRows(stored.rows);
      secretsState.remember(stored.created); // a retry after a later failure sees them
      if (stored.failed) {
        setRowErrors({ [stored.failed.rid]: { variable: stored.failed.message } });
        return;
      }
      await secretsState.reload();
      const next = await api.updateMcpServer(
        server.id,
        {
          settings: settingsFromRows(stored.rows, server.transport),
          ...(server.transport === "stdio" ? { args: argsFromText(argsText) } : {}),
        },
        server.updated_at,
      );
      setServer(next);
      setDirty(false);
      setStep(3);
      void runCheck(next.id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // ---- step 4: check
  const [check, setCheck] = useState<McpCheckRow | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const runCheck = async (id = server?.id) => {
    if (!id) return;
    setChecking(true);
    setCheckError("");
    try {
      setCheck(await api.checkMcpServer(id, "shared"));
    } catch (e) {
      setCheckError(errorText(e));
    } finally {
      setChecking(false);
    }
  };

  // ---- step 5: enable
  const [detail, setDetail] = useState<McpServerDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [mode, setMode] = useState<"all" | "selected">("selected");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<Record<string, string>>({});
  const [enableDone, setEnableDone] = useState(false);
  const loadDetail = useCallback(async () => {
    if (!server) return null;
    try {
      const d = await api.mcpServer(server.id);
      setDetail(d);
      setDetailError("");
      return d;
    } catch (e) {
      setDetailError(errorText(e));
      return null;
    }
  }, [server]);
  /** Arriving at Enable: read the per-project state, and pre-tick every project that can be on.
   *  From then on the ticks are the operator's. */
  const arriveAtEnable = async () => {
    setStep(4);
    setDetail(null);
    const d = await loadDetail();
    if (!d) return;
    const shared = sharedAvailable(d);
    setChosen(new Set(d.status.projects.filter((r) => enableAction(r, shared) !== "blocked").map((r) => r.project_id)));
  };
  const actions = useMemo(() => {
    const out = new Map<string, EnableAction>();
    if (!detail) return out;
    const shared = sharedAvailable(detail);
    for (const r of detail.status.projects) out.set(r.project_id, enableAction(r, shared));
    return out;
  }, [detail]);
  const targets = useMemo(
    () =>
      detail
        ? detail.status.projects
            .map((r) => r.project_id)
            .filter((p) => actions.get(p) !== "blocked" && (mode === "all" || chosen.has(p)))
        : [],
    [detail, actions, mode, chosen],
  );

  const enable = async () => {
    if (!server || !detail) return;
    setBusy(true);
    setError("");
    const words: Record<string, string> = {};
    setReport({});
    try {
      const bindRows = detail.status.projects.filter((r) => actions.get(r.project_id) === "bind");
      await bindShared(bindPlan(bindRows, new Set(targets)));
      let d = (await loadDetail()) ?? detail;
      const passed = new Set<string>();
      for (const pid of targets) {
        const row = d.status.projects.find((r) => r.project_id === pid);
        if (!row) continue;
        if (row.check === "passed") {
          passed.add(pid);
          continue;
        }
        if (row.credentials === "missing") {
          words[pid] = "Still no credentials: left off.";
          continue;
        }
        words[pid] = "Checking…";
        setReport({ ...words });
        try {
          const c = await api.checkMcpServer(server.id, pid);
          if (c.outcome === "passed") {
            passed.add(pid);
            delete words[pid];
          } else {
            const bad = c.steps.find((s) => !s.ok && s.step !== "stderr");
            words[pid] = `${verdict(c).words}${bad ? `: ${bad.detail}` : ""}. Left off.`;
          }
        } catch (e) {
          words[pid] = `${errorText(e)}. Left off.`;
        }
        setReport({ ...words });
      }
      d = (await loadDetail()) ?? d;
      const projectsValue = mode === "all" ? "all" : finalProjects(targets, passed);
      const next = await api.setMcpProjects(server.id, projectsValue, d.server.updated_at);
      setServer(next);
      await loadDetail();
      setReport(words);
      setEnableDone(true);
      if (Object.keys(words).length === 0) {
        navigate(mcpServerPath(server.id), {
          state: {
            flash:
              projectsValue === "all"
                ? "Enabled for all projects. Running sessions pick it up when they next start."
                : `Enabled in ${projectsValue.length} project${projectsValue.length === 1 ? "" : "s"}. Running sessions pick it up when they next start.`,
          },
        });
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // ---- navigation
  const goNext = () => {
    if (step === 0) void saveSource();
    else if (step === 1) {
      if (isPackage && !installed) void install();
      else {
        if (server && rows.length === 0 && server.settings.length) setRows(rowsFromSettings(server.settings));
        setStep(2);
      }
    } else if (step === 2) void saveSettings();
    else if (step === 3) void arriveAtEnable();
    else void enable();
  };
  const nextLabel = [
    busy ? "Saving…" : "Next: Review",
    isPackage && !installed ? (busy ? "Installing…" : "Install") : "Next: Settings",
    busy ? "Saving…" : "Save & check",
    "Next: Enable",
    busy ? "Enabling…" : "Enable",
  ][step];
  const nextDisabled =
    busy ||
    (step === 1 && isPackage && !installed && !facts) ||
    (step === 3 && (checking || !check)) ||
    (step === 4 && (!detail || (mode === "selected" && targets.length === 0)));
  // The shell draws `nextNote` in danger ink (it says why Next may not do what you hope), so it is
  // only the failed check's warning; the plain facts live in the panels.
  const nextNote =
    step === 3 && check && check.outcome !== "passed"
      ? "The shared check did not pass: projects using the shared credentials stay off until it does."
      : undefined;

  const leaveGuard =
    (step === 0 && !server && !!(url || pkg || version)) || (step === 2 && dirty);

  const scopeLabel = "shared credentials";
  const body = (() => {
    switch (step) {
      case 0:
        return (
          <Panel rail="short" faceClassName={styles.face}>
            <Callsign title="Source" value={{ remote: "remote server", npm: "npm package", pypi: "PyPI package" }[kind]} />
            <fieldset className={styles.choices} data-testid="mcp-source-kinds">
              <legend className="nj-sr-only">Source kind</legend>
              {(
                [
                  ["remote", Globe, "Remote server (URL)", "Someone else runs it. Sessions connect over HTTPS."],
                  ["npm", Box, "npm package", "Installed once into BattleLab’s own folder; each session starts it over stdio."],
                  ["pypi", Package, "PyPI package", "Installed with uv into its own environment, wheels only; started over stdio."],
                ] as const
              ).map(([value, Icon, title, text]) => (
                <label key={value} className={styles.choice}>
                  <input
                    type="radio"
                    name="mcp-kind"
                    value={value}
                    checked={kind === value}
                    disabled={busy}
                    onChange={() => setKind(value)}
                  />
                  <span className={styles.choiceHead}>
                    <Icon size={16} aria-hidden="true" />
                    {title}
                  </span>
                  <p>{text}</p>
                </label>
              ))}
              <label className={styles.choice}>
                <input type="radio" name="mcp-kind" value="git" disabled aria-describedby="mcp-git-later" />
                <span className={styles.choiceHead}>
                  <GitBranch size={16} aria-hidden="true" />
                  Git repository <span className={styles.tag}>Later</span>
                </span>
                <p id="mcp-git-later">Build from a pinned commit. Not available yet.</p>
              </label>
            </fieldset>
            <div className={styles.fields}>
              {kind === "remote" ? (
                <Field
                  className={styles.full}
                  label="Server URL"
                  mono
                  type="url"
                  inputMode="url"
                  placeholder="https://mcp.example.com/mcp"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  hint="Streamable HTTP. https only; credentials go in a setting, never in the URL."
                  error={tried && sourceErrors.url ? sourceErrors.url : undefined}
                />
              ) : (
                <>
                  <Field
                    label="Package"
                    mono
                    value={pkg}
                    onChange={(e) => setPkg(e.target.value)}
                    hint={kind === "npm" ? "The name on registry.npmjs.org." : "The name on pypi.org."}
                    error={tried && sourceErrors.pkg ? sourceErrors.pkg : undefined}
                  />
                  <Field
                    label="Version"
                    mono
                    value={version}
                    onChange={(e) => setVersion(e.target.value)}
                    hint="An exact version. Ranges and ‘latest’ are refused."
                    error={tried && sourceErrors.version ? sourceErrors.version : undefined}
                  />
                  {kind === "pypi" && (
                    <Field
                      className={styles.full}
                      label="Entry point (optional)"
                      mono
                      value={entrypoint}
                      onChange={(e) => setEntrypoint(e.target.value)}
                      hint="The console script to start, when the package has more than one."
                    />
                  )}
                </>
              )}
              <Field
                label="Name"
                mono
                value={name}
                onChange={(e) => {
                  setNameInput(e.target.value);
                  setNameTouched(true);
                }}
                hint={
                  <>
                    Agents see its tools as <code>{name || "name"}</code> · … Remove a hand-registered server of the
                    same name from your CLI’s own config.
                  </>
                }
                error={tried && sourceErrors.name ? sourceErrors.name : undefined}
              />
              <Field
                label="Label (optional)"
                value={label}
                maxLength={80}
                onChange={(e) => setLabel(e.target.value)}
                hint="How the Library shows it, e.g. Nextcloud."
              />
            </div>
          </Panel>
        );
      case 1:
        return (
          <Panel rail="short" faceClassName={styles.face}>
            <Callsign title="What will run" value={server?.name} />
            {!server ? null : !isPackage ? (
              <>
                <RemoteFacts url={(server.source as { url: string }).url} />
                <Caution title="Third-party server">
                  Sessions send it their tool calls, and it receives the credentials you set for it. Only add a server
                  you trust.
                </Caution>
              </>
            ) : installed ? (
              <p className={styles.note} role="status">
                <StateWords tone="up" words="Installed" /> This exact version is installed. Continue to its settings.
              </p>
            ) : reviewError ? (
              <div className={styles.alert} role="alert">
                <h2>Couldn’t review the package</h2>
                <p>{reviewError}</p>
                <button type="button" className={styles.ghost} onClick={() => void runReview()}>
                  <RunAgainLabel busy={false} label="Review again" />
                </button>
              </div>
            ) : !facts ? (
              <>
                <Loader kind="steps" label="Resolving the exact version into a lock" />
                <p className={styles.note} data-testid="mcp-op-note">
                  On a busy host this can take a minute or two.
                </p>
              </>
            ) : (
              <>
                <ReviewFacts facts={facts} />
                <Caution title="Third-party code">
                  It runs as your user inside every session that gets this server. Review before installing.
                </Caution>
                {busy ? (
                  <p className={styles.note} role="status" data-testid="mcp-op-note">
                    Installing exactly this lock. A package can take a few minutes; the install carries on even if you
                    leave this page.
                  </p>
                ) : (
                  <p className={styles.note}>Nothing has been downloaded yet: Install fetches exactly this lock.</p>
                )}
              </>
            )}
          </Panel>
        );
      case 2:
        return (
          <Panel rail="short" faceClassName={styles.face}>
            <Callsign title="Settings & credentials" value="shared" />
            {secretsState.error && <p className={styles.error}>Variables can’t be read: {secretsState.error}</p>}
            {server && (
              <SettingsEditor
                rows={rows}
                onChange={editRows}
                transport={server.transport}
                serverName={server.name}
                secrets={secretsState.secrets}
                errors={rowErrors}
                disabled={busy}
              />
            )}
            {server?.transport === "stdio" && (
              <Field
                as="textarea"
                label="Arguments (optional)"
                mono
                rows={2}
                value={argsText}
                onChange={(e) => {
                  setArgsText(e.target.value);
                  setDirty(true);
                }}
                hint="Added to the server’s command, one per line. Literal: never run through a shell."
              />
            )}
            <p className={styles.note}>
              These are the shared values. A project can use its own on the server’s <b>Projects</b> tab.
            </p>
          </Panel>
        );
      case 3:
        return (
          <Panel rail="short">
            <div className={styles.face}>
              <Callsign title="Check" value={scopeLabel} />
            </div>
            {checkError ? (
              <div className={styles.face}>
                <p className={styles.error} role="alert">
                  {checkError}
                </p>
                <div className={styles.btnRow}>
                  <button type="button" className={styles.ghost} onClick={() => void runCheck()} disabled={checking}>
                    <RunAgainLabel busy={checking} />
                  </button>
                </div>
              </div>
            ) : checking && !check ? (
              <div className={styles.face}>
                <Loader kind="steps" label="Starting the server and listing its tools" />
              </div>
            ) : check ? (
              <CheckReport
                row={check}
                scopeLabel={scopeLabel}
                actions={
                  <>
                    {check.outcome !== "passed" && (
                      <button type="button" className={styles.primary} onClick={() => setStep(2)}>
                        <KeyRound size={14} aria-hidden="true" />
                        Edit credentials
                      </button>
                    )}
                    <button type="button" className={styles.ghost} onClick={() => void runCheck()} disabled={checking}>
                      <RunAgainLabel busy={checking} />
                    </button>
                  </>
                }
              />
            ) : (
              <div className={styles.face}>
                <button type="button" className={styles.primary} onClick={() => void runCheck()}>
                  Run check
                </button>
              </div>
            )}
            <div className={styles.face}>
              <p className={styles.note}>
                A probe call (one tool BattleLab calls to prove it answers) can be added on the server’s Settings tab
                once you know its tools.
              </p>
            </div>
          </Panel>
        );
      default:
        return (
          <Panel rail="short" faceClassName={styles.face}>
            <Callsign
              title="Enable for"
              value={
                detail
                  ? mode === "all"
                    ? "all projects"
                    : `${targets.length} of ${detail.status.projects.length} projects`
                  : undefined
              }
            />
            <Seg<"all" | "selected">
              label="Enable for"
              value={mode}
              onChange={setMode}
              testId="mcp-enable-mode"
              options={[
                { value: "all", label: "All projects" },
                { value: "selected", label: "Selected" },
              ]}
            />
            <p className={styles.note}>
              {mode === "all"
                ? "All projects also covers projects you add later — each one that has credentials and a passing check."
                : "Tick the projects whose sessions get this server."}
            </p>
            {detailError && (
              <p className={styles.error} role="alert">
                {detailError}
              </p>
            )}
            {!detail ? (
              !detailError && <Loader kind="skeleton" label="Acquiring projects" rows={3} />
            ) : detail.status.projects.length === 0 ? (
              <p className={styles.note}>No projects yet. Create one, then enable the server for it here or on its page.</p>
            ) : (
              <ul className={styles.ptbl} aria-label="Projects">
                <li className={`${styles.phead} ${styles.enableRow}`} aria-hidden="true">
                  <span>Project</span>
                  <span>Credentials</span>
                  <span>Check</span>
                  <span>Enable</span>
                </li>
                {detail.status.projects.map((r) => {
                  const act = actions.get(r.project_id) ?? "blocked";
                  const cv = checkView(r.check, r.checked_at);
                  const pname = projectName(r.project_id);
                  const on = act !== "blocked" && (mode === "all" || chosen.has(r.project_id));
                  return (
                    <li
                      key={r.project_id}
                      className={`${styles.prow} ${styles.enableRow}`}
                      data-testid="mcp-enable-row"
                      data-project={r.project_id}
                      data-action={act}
                    >
                      <span className={styles.pname}>
                        <ProjectSquare color={color(r.project_id)} />
                        {pname}
                        {act === "blocked" && <small>uses its own credentials only</small>}
                      </span>
                      <span className={styles.cell}>
                        <span className={styles.cellLabel}>Credentials</span>
                        {act === "bind" ? (
                          <span className={styles.cred}>
                            Shared <small>bound on Enable</small>
                          </span>
                        ) : act === "blocked" ? (
                          <StateWords tone="att" words="Missing" />
                        ) : (
                          <span className={styles.cred}>{credentialWords(r.credentials)}</span>
                        )}
                      </span>
                      <span className={styles.cell}>
                        <span className={styles.cellLabel}>Check</span>
                        {report[r.project_id] ? (
                          <span className={styles.note} role="status">
                            {report[r.project_id]}
                          </span>
                        ) : act === "blocked" ? (
                          <span className={styles.note}>—</span>
                        ) : r.check === "passed" || (act === "bind" && check?.outcome === "passed") ? (
                          <StateWords tone="up" words={r.check === "passed" ? cv.words : "Passed (shared)"} />
                        ) : (
                          <>
                            <StateWords tone="idle" words="Not run" />
                            <span className={styles.note}>runs on Enable</span>
                          </>
                        )}
                      </span>
                      <span className={styles.cell}>
                        {act === "blocked" ? (
                          <Link className={styles.linkBtn} to={`${mcpServerPath(server?.id ?? "")}?tab=projects`}>
                            Add credentials first
                          </Link>
                        ) : (
                          <label className={styles.pick}>
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={busy || mode === "all"}
                              aria-label={`Enable for ${pname}`}
                              onChange={(e) =>
                                setChosen((prev) => {
                                  const next = new Set(prev);
                                  if (e.target.checked) next.add(r.project_id);
                                  else next.delete(r.project_id);
                                  return next;
                                })
                              }
                            />
                            <span>{on ? "On" : "Off"}</span>
                          </label>
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            <p className={styles.note}>
              A project whose credentials have not passed a check yet is checked when you press Enable; if that check
              fails it stays off and says why. Using the shared credentials binds them to the project in Variables —
              agents still cannot read them. Running sessions pick this up when they next start.
            </p>
            {enableDone && server && (
              <div className={styles.btnRow}>
                <Link className={styles.ghost} to={mcpServerPath(server.id)}>
                  <Power size={14} aria-hidden="true" />
                  Open the server page
                </Link>
              </div>
            )}
          </Panel>
        );
    }
  })();

  return (
    <WizardShell
      kicker="Add MCP server"
      steps={STEPS}
      current={step}
      wide
      canJump={(i) => i < step && !busy}
      onJump={setStep}
      heading={HEADINGS[step]}
      onBack={step > 0 ? () => setStep(step - 1) : undefined}
      onNext={goNext}
      nextLabel={nextLabel}
      nextDisabled={nextDisabled}
      nextNote={nextNote}
      leaveGuard={leaveGuard}
      leaveTitle="Leave the MCP server setup?"
      secondary={
        server ? (
          <Link className={styles.ghost} to={mcpServerPath(server.id)}>
            Finish later
          </Link>
        ) : (
          <Link className={styles.ghost} to={MCP_PATH}>
            Cancel
          </Link>
        )
      }
    >
      <div className={styles.stepGrid} data-testid={`mcp-step-${STEPS[step].id}`}>
        <div className={styles.valueCell}>
          {body}
          {error && (
            <p className={styles.error} role="alert" data-testid="mcp-wizard-error">
              {error}
            </p>
          )}
        </div>
        <Panel rail="short" faceClassName={styles.face} className={styles.aside}>
          <Callsign title="What happens next" />
          <WhatHappensNext current={step} />
          {projects && projects.length === 0 && step === 4 && (
            <p className={styles.note}>You have no projects yet.</p>
          )}
        </Panel>
      </div>
    </WizardShell>
  );
}
