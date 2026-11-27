/** Library → MCP servers → one server (#2163 P1c): Overview (its tools, from the newest passing
 *  check) · Projects (credentials Own / Shared / Missing, the check, the switch, per project) ·
 *  Settings (the shared values, arguments, the probe, a new version) · Activity (check history).
 *  The ⋯ menu disables, enables and removes it.
 *
 *  A changed credential or setting makes a project's check stale: it shows "Credentials changed —
 *  check again" (amber), its switch stays on, and the broker delivers nothing until a check passes.
 *  Running sessions keep what they started with until they exit; the page says so. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, GitBranch, KeyRound, MoreHorizontal, Plug, Power, RefreshCw, Trash2, Upload, Package } from "lucide-react";

import { api } from "../lib/api";
import { MCP_ID_RE, MCP_PATH, mcpServerUpdatePath } from "../lib/routes";
import type { McpProjectStatus, McpReviewFacts, McpServerDetail, McpSource } from "../types/mcp";
import { Callsign, Panel } from "../components/hud/Panel";
import { Disclosure, Field, Switch, Tabs } from "../components/hud/Controls";
import { Loader } from "../components/hud/Loader";
import { RowMenu, type RowMenuEntry } from "../components/sidebar/RowMenu";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { Caution, CheckReport, ProjectSquare, ReviewFacts, RunAgainLabel, StateWords } from "../components/mcp/McpBits";
import { SettingsEditor } from "../components/mcp/SettingsEditor";
import { useMcpOperation } from "../components/mcp/mcpOperation";
import { OriginNote } from "../components/mcp/RepoLink";
import { bindShared, errorText, storeNewCredentials, useProjects, useSecrets } from "../components/mcp/mcpData";
import {
  SECRET_MIN,
  argsFromText,
  checkView,
  credentialWords,
  currentTools,
  projectsSummary,
  rowsFromSettings,
  settingsFromRows,
  sharedAvailable,
  sourceChip,
  stateView,
  toggledProjects,
  urlHost,
  validateRows,
  verdict,
  when,
  type RowErrors,
  type SettingRow,
} from "../components/mcp/mcpModel";
import styles from "../components/mcp/mcp.module.css";

type Tab = "overview" | "projects" | "settings" | "activity";
const TABS: Tab[] = ["overview", "projects", "settings", "activity"];

const sameSource = (a: McpSource | undefined, b: McpSource | undefined) => JSON.stringify(a) === JSON.stringify(b);

/** One mounted page per server: everything the page holds (the record, its errors, a tab's form,
 *  an open dialog) belongs to exactly the server in the URL. Navigating to another server mounts a
 *  fresh page, and a response still in flight for the old one lands on an unmounted page. */
export default function McpServer() {
  const { serverId = "" } = useParams();
  return <McpServerPage key={serverId} serverId={serverId} />;
}

function McpServerPage({ serverId }: { serverId: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab") as Tab | null;
  const tab: Tab = tabParam && TABS.includes(tabParam) ? tabParam : "overview";
  const setTab = (t: Tab) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (t === "overview") next.delete("tab");
        else next.set("tab", t);
        return next;
      },
      { replace: true },
    );
  const { name: projectName, color } = useProjects();
  const [detail, setDetail] = useState<McpServerDetail | null>(null);
  const [error, setError] = useState("");
  const [notFound, setNotFound] = useState(false);
  const [flash, setFlash] = useState<string>(
    (location.state as { flash?: string } | null)?.flash ?? "",
  );
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [ownFor, setOwnFor] = useState<McpProjectStatus | null>(null);

  const badId = !MCP_ID_RE.test(serverId);
  // The newest request wins: a slower, older read (an action's reload racing the first load) never
  // replaces a newer answer, and nothing lands after the page is gone.
  const seq = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const load = useCallback((): Promise<McpServerDetail | null> => {
    if (!MCP_ID_RE.test(serverId)) return Promise.resolve(null);
    const mine = ++seq.current;
    const current = () => mounted.current && mine === seq.current;
    return api.mcpServer(serverId).then(
      (d) => {
        if (!current() || d.server.id !== serverId) return null;
        setDetail(d);
        setError("");
        setNotFound(false);
        return d;
      },
      (e: unknown) => {
        if (!current()) return null;
        if ((e as { status?: number }).status === 404) setNotFound(true);
        else setError(errorText(e));
        return null;
      },
    );
  }, [serverId]);
  useEffect(() => {
    void load();
  }, [load]);

  const act = async (key: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(key);
    setActionError("");
    try {
      await fn();
      if (done) setFlash(done);
    } catch (e) {
      setActionError(errorText(e));
    } finally {
      await load();
      setBusy("");
    }
  };

  if (notFound || badId)
    return (
      <div className={styles.page} data-testid="mcp-server-missing">
        <div className={styles.wrap}>
          <Link className={styles.back} to={MCP_PATH}>
            <ArrowLeft size={14} aria-hidden="true" /> MCP servers
          </Link>
          <div className={styles.empty}>
            <h2>This MCP server isn’t here</h2>
            <p className={styles.note}>It may have been removed. The Library lists every server that is.</p>
          </div>
        </div>
      </div>
    );

  if (!detail)
    return (
      <div className={styles.page}>
        <div className={styles.wrap}>
          <Link className={styles.back} to={MCP_PATH}>
            <ArrowLeft size={14} aria-hidden="true" /> MCP servers
          </Link>
          {error ? (
            <div className={styles.alert} role="alert" data-testid="mcp-read-error">
              <h2>Couldn’t load this MCP server</h2>
              <p>{error}</p>
              <button type="button" className={styles.ghost} onClick={() => void load()}>
                <RefreshCw size={14} aria-hidden="true" /> Retry
              </button>
            </div>
          ) : (
            <Loader kind="skeleton" label="Acquiring the server" rows={4} />
          )}
        </div>
      </div>
    );

  const s = detail.server;
  const st = stateView(s.state, projectName);
  const tools = currentTools(detail.checks, s.rev);
  const shared = sharedAvailable(detail);
  const every = detail.status.projects.map((r) => r.project_id);
  const enabledCount = detail.status.projects.filter((r) => r.enabled).length;

  const runAll = () =>
    act("all", async () => {
      let n = 0;
      const total = 1 + detail.status.projects.filter((r) => r.credentials === "own").length;
      setFlash(`Checking ${++n} of ${total}…`);
      await api.checkMcpServer(s.id, "shared");
      const fresh = await api.mcpServer(s.id);
      for (const r of fresh.status.projects) {
        if (r.credentials === "missing" || r.check === "passed") continue;
        setFlash(`Checking ${++n} of ${Math.max(total, n)}…`);
        await api.checkMcpServer(s.id, r.project_id);
      }
      setFlash("Checks finished.");
    });

  const menu: RowMenuEntry[] = [
    s.enabled
      ? {
          key: "disable",
          label: "Disable",
          icon: <Power size={15} />,
          onSelect: () =>
            void act("toggle", () => api.updateMcpServer(s.id, { enabled: false }, s.updated_at), "Disabled. No new session gets its tools; running ones keep them until they restart."),
        }
      : {
          key: "enable",
          label: "Enable again",
          icon: <Power size={15} />,
          onSelect: () => void act("toggle", () => api.updateMcpServer(s.id, { enabled: true }, s.updated_at), "Enabled again."),
        },
    // A server added from a repository link updates from that link (#2427): read again, old → new.
    s.origin
      ? {
          key: "update-source",
          label: "Update from source…",
          icon: <GitBranch size={15} />,
          onSelect: () => navigate(mcpServerUpdatePath(s.id)),
        }
      : {
          key: "update",
          label: s.transport === "stdio" ? "Update…" : "Edit source…",
          icon: <Upload size={15} />,
          onSelect: () => setTab("settings"),
        },
    "separator",
    { key: "remove", label: "Remove…", icon: <Trash2 size={15} />, danger: true, onSelect: () => setConfirmRemove(true) },
  ];

  return (
    <div className={styles.page} data-testid="mcp-server-page" data-server={s.name}>
      <div className={styles.wrap}>
        <Link className={styles.back} to={MCP_PATH}>
          <ArrowLeft size={14} aria-hidden="true" /> MCP servers
        </Link>
        <div className={styles.headRow}>
          <div className={styles.titleRow}>
            <span className={`${styles.mark} ${styles.markLg}`} aria-hidden="true">
              <Plug size={20} />
            </span>
            <div>
              <p className={styles.kicker}>
                <span className="nj-notch" aria-hidden="true" />
                Library <span aria-hidden="true">//</span> MCP servers <span aria-hidden="true">//</span>{" "}
                <b>{s.name}</b>
              </p>
              <h1 className={styles.h1}>{s.label || s.name}</h1>
            </div>
          </div>
          <div className={styles.headActs}>
            <button type="button" className={styles.ghost} onClick={() => void runAll()} disabled={!!busy}>
              <RunAgainLabel busy={busy === "all"} label="Run all checks" />
            </button>
            <RowMenu
              items={menu}
              title={s.label || s.name}
              sheetTitle="Server actions"
              triggerLabel={`More for ${s.name}`}
              triggerIcon={<MoreHorizontal size={16} aria-hidden="true" />}
              triggerClassName={styles.iconBtn}
              triggerTestId="mcp-server-menu"
            />
          </div>
        </div>
        <div className={styles.chips}>
          <StateWords tone={st.tone} words={st.words} testId="mcp-server-state" />
          <span className={styles.chip}>
            <Package size={13} aria-hidden="true" />
            {sourceChip(s.source)}
          </span>
          <span className={styles.chip}>{tools.length || s.tool_count} tools</span>
          <span className={styles.chip}>{projectsSummary(s)}</span>
        </div>
        {flash && (
          <p className={styles.status} role="status" data-testid="mcp-flash">
            {flash}
          </p>
        )}
        {actionError && (
          <p className={styles.error} role="alert" data-testid="mcp-action-error">
            {actionError}
          </p>
        )}

        {!s.enabled && (
          <Panel rail="short" faceClassName={styles.face} data-testid="mcp-disabled">
            <Callsign title="Disabled" />
            <p className={styles.note}>
              Settings, credentials and the per-project choices ({enabledCount} of {every.length}) are kept. No new
              session gets its tools; sessions already running keep them until they restart.
            </p>
            <div className={styles.btnRow}>
              <button
                type="button"
                className={styles.primary}
                disabled={!!busy}
                onClick={() => void act("toggle", () => api.updateMcpServer(s.id, { enabled: true }, s.updated_at), "Enabled again.")}
              >
                <Power size={14} aria-hidden="true" /> Enable again
              </button>
              <button type="button" className={styles.ghost} onClick={() => setConfirmRemove(true)}>
                <Trash2 size={14} aria-hidden="true" /> Remove…
              </button>
            </div>
          </Panel>
        )}

        <Tabs<Tab>
          label="Server sections"
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "projects", label: `Projects ${every.length}` },
            { id: "settings", label: "Settings" },
            { id: "activity", label: "Activity" },
          ]}
          panel={(t) =>
            t === "overview" ? (
              <Overview detail={detail} tools={tools} onCheck={() => void act("shared", () => api.checkMcpServer(s.id, "shared"))} busy={busy === "shared"} />
            ) : t === "projects" ? (
              <Panel rail="short" faceClassName={styles.face}>
                <Callsign title="Projects" value={`${enabledCount} of ${every.length} enabled`} />
                {every.length === 0 ? (
                  <p className={styles.note}>No projects yet.</p>
                ) : (
                  <ul className={styles.ptbl} aria-label="Projects">
                    <li className={styles.phead} aria-hidden="true">
                      <span>Project</span>
                      <span>Credentials</span>
                      <span>Last check</span>
                      <span>Enabled</span>
                      <span />
                    </li>
                    {detail.status.projects.map((r) => {
                      const cv = checkView(r.check, r.checked_at);
                      const pname = projectName(r.project_id);
                      const blocked = r.credentials === "missing";
                      return (
                        <li key={r.project_id} className={styles.prow} data-testid="mcp-project-row" data-project={r.project_id} data-check={r.check}>
                          <span className={styles.pname}>
                            <ProjectSquare color={color(r.project_id)} />
                            {pname}
                          </span>
                          <span className={styles.cell}>
                            <span className={styles.cellLabel}>Credentials</span>
                            {blocked ? (
                              <StateWords tone="att" words="Missing" />
                            ) : (
                              <span className={styles.cred}>{credentialWords(r.credentials)}</span>
                            )}
                          </span>
                          <span className={styles.cell}>
                            <span className={styles.cellLabel}>Last check</span>
                            {blocked ? <span className={styles.note}>— not run</span> : <StateWords tone={cv.tone} words={cv.words} />}
                          </span>
                          <span className={styles.cell}>
                            <span className={styles.cellLabel}>Enabled</span>
                            <Switch
                              checked={r.enabled}
                              disabled={blocked || !!busy}
                              label={`Enabled for ${pname}`}
                              testId={`mcp-switch-${r.project_id}`}
                              onChange={(on) =>
                                void act(`sw-${r.project_id}`, () =>
                                  api.setMcpProjects(s.id, toggledProjects(s.projects, every, r.project_id, on), s.updated_at),
                                )
                              }
                            />
                          </span>
                          <span className={styles.acts}>
                            {blocked && shared && (
                              <button
                                type="button"
                                className={`${styles.ghost} ${styles.compact}`}
                                disabled={!!busy}
                                onClick={() =>
                                  void act(
                                    `bind-${r.project_id}`,
                                    () => bindShared(Object.fromEntries(r.missing.map((v) => [v, [r.project_id]]))),
                                    `${pname} uses the shared credentials now. Run its check before switching it on.`,
                                  )
                                }
                              >
                                Use shared
                              </button>
                            )}
                            {r.credentials !== "none_needed" && (
                              <button
                                type="button"
                                className={`${styles.ghost} ${styles.compact}`}
                                disabled={!!busy}
                                onClick={() => setOwnFor(r)}
                              >
                                {blocked && <KeyRound size={14} aria-hidden="true" />}
                                {r.credentials === "own" ? "Edit own credentials" : blocked ? "Add own credentials" : "Use own credentials"}
                              </button>
                            )}
                            {!blocked && (
                              <button
                                type="button"
                                className={`${r.check === "stale" || r.check === "not_run" ? styles.primary : styles.ghost} ${styles.compact}`}
                                disabled={!!busy}
                                onClick={() => void act(`check-${r.project_id}`, () => api.checkMcpServer(s.id, r.project_id))}
                              >
                                <RunAgainLabel busy={busy === `check-${r.project_id}`} label="Run check" />
                              </button>
                            )}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                )}
                <p className={styles.note}>
                  Each project uses the <b>shared</b> credentials or its <b>own</b>; a project with neither doesn’t get
                  the server. Changing a project’s credentials asks for a new check. Running sessions pick changes up
                  when they next start; a server already running keeps the values it started with.
                </p>
              </Panel>
            ) : t === "settings" ? (
              <SettingsTab key={`${s.id}-${s.updated_at}`} detail={detail} tools={tools} onSaved={(m) => { setFlash(m); void load(); }} />
            ) : (
              <Activity detail={detail} projectName={projectName} />
            )
          }
        />
      </div>

      {confirmRemove && (
        <ConfirmDialog
          tag="Remove MCP server"
          title={`Remove ${s.label || s.name}?`}
          confirmLabel="Remove"
          danger
          busy={busy === "remove"}
          onCancel={() => setConfirmRemove(false)}
          onConfirm={() => {
            setBusy("remove");
            api.deleteMcpServer(s.id, s.updated_at).then(
              () => navigate(MCP_PATH),
              (e: unknown) => {
                setActionError(errorText(e));
                setConfirmRemove(false);
                setBusy("");
                void load();
              },
            );
          }}
        >
          <p>
            This removes its registration: its settings, project choices and check history. Its credentials stay in
            Variables, and its installed files stay on disk. No new session gets its tools; sessions already running
            keep them until they exit.
          </p>
        </ConfirmDialog>
      )}
      {ownFor && (
        <OwnCredentialsDialog
          serverId={s.id}
          row={ownFor}
          variables={[...new Set(s.settings.flatMap((x) => (x.kind === "credential" ? [x.variable] : [])))]}
          projectName={projectName(ownFor.project_id)}
          onClose={(saved) => {
            setOwnFor(null);
            if (saved) {
              setFlash(`Saved ${projectName(ownFor.project_id)}’s own credentials. Run its check: nothing is delivered there until it passes.`);
              void load();
            }
          }}
        />
      )}
    </div>
  );
}

function Overview({
  detail,
  tools,
  onCheck,
  busy,
}: {
  detail: McpServerDetail;
  tools: string[];
  onCheck: () => void;
  busy: boolean;
}) {
  const s = detail.server;
  const src = s.source;
  const installed = s.installed as { source?: McpSource; integrity?: string; installed_at?: number } | null;
  return (
    <div className={styles.stepGrid}>
      <Panel rail="short" faceClassName={styles.face} data-testid="mcp-tools">
        <Callsign title="Tools" value={tools.length ? `${tools.length}` : "none checked yet"} />
        {tools.length ? (
          <>
            <ul className={styles.toolList} aria-label="Tools">
              {tools.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
            <p className={styles.note}>
              From the newest passing check. Agents see them as <code>{s.name}</code> · <code>{tools[0]}</code>.
            </p>
          </>
        ) : (
          <>
            <p className={styles.note}>A check that passes lists the server’s tools here.</p>
            <div className={styles.btnRow}>
              <button type="button" className={styles.primary} onClick={onCheck} disabled={busy}>
                <RunAgainLabel busy={busy} label="Run check" />
              </button>
            </div>
          </>
        )}
      </Panel>
      <Panel rail="short" faceClassName={styles.face}>
        <Callsign title="Source" value={src.kind} />
        <dl className={styles.kv}>
          {src.kind === "remote" ? (
            <>
              <dt>URL</dt>
              <dd className={styles.mono}>{src.url}</dd>
              <dt>Host</dt>
              <dd className={styles.mono}>{urlHost(src.url)}</dd>
            </>
          ) : (
            <>
              <dt>{src.kind === "go" ? "Module" : "Package"}</dt>
              <dd className={styles.mono}>{src.package}</dd>
              <dt>Version</dt>
              <dd className={styles.mono}>{src.version}</dd>
              <dt>Install</dt>
              <dd>
                {installed && sameSource(installed.source, src) ? (
                  <StateWords tone="up" words={`Installed ${when(installed.installed_at)}`} />
                ) : (
                  <StateWords tone="att" words="Not installed — review and install on Settings" />
                )}
              </dd>
            </>
          )}
          <dt>Transport</dt>
          <dd>{s.transport === "http" ? "Streamable HTTP" : "stdio · started per session"}</dd>
          <dt>Shared check</dt>
          <dd>
            <StateWords tone={detail.status.shared.check === "passed" ? "up" : "idle"} words={detail.status.shared.check === "passed" ? "Passed" : "Not passed for the current values"} />
          </dd>
        </dl>
      </Panel>
    </div>
  );
}

function Activity({ detail, projectName }: { detail: McpServerDetail; projectName: (id: string) => string }) {
  if (!detail.checks.length)
    return (
      <Panel rail="short" faceClassName={styles.face}>
        <Callsign title="Activity" />
        <p className={styles.note}>No checks yet.</p>
      </Panel>
    );
  return (
    <Panel rail="short" faceClassName={styles.face}>
      <Callsign title="Activity" value="checks, newest first" />
      <ol className={styles.activity} data-testid="mcp-activity">
        {detail.checks.map((c, i) => {
          const v = verdict(c);
          const scope = c.scope === "shared" ? "Shared credentials" : projectName(c.scope);
          return (
            <li key={`${c.at}-${i}`} className={styles.actRow} data-outcome={c.outcome}>
              <div className={styles.actHead}>
                <span className={styles.mono}>{when(c.at)}</span>
                <span>{scope}</span>
                <StateWords tone={v.tone} words={v.words} />
                <span className={styles.note}>revision {c.rev}{c.rev !== detail.server.rev ? " (an earlier one)" : ""}</span>
              </div>
              <Disclosure summary="Steps">
                <CheckReport row={c} scopeLabel={c.scope === "shared" ? "shared credentials" : `credentials of ${scope}`} />
              </Disclosure>
            </li>
          );
        })}
      </ol>
    </Panel>
  );
}

function SettingsTab({
  detail,
  tools,
  onSaved,
}: {
  detail: McpServerDetail;
  tools: string[];
  onSaved: (message: string) => void;
}) {
  const s = detail.server;
  const secretsState = useSecrets();
  const [rows, setRows] = useState<SettingRow[]>(() => rowsFromSettings(s.settings));
  const [rowErrors, setRowErrors] = useState<Record<string, RowErrors>>({});
  const [argsText, setArgsText] = useState(s.args.join("\n"));
  const [url, setUrl] = useState(s.source.kind === "remote" ? s.source.url : "");
  const [version, setVersion] = useState(s.source.kind !== "remote" ? s.source.version : "");
  const [probeTool, setProbeTool] = useState(s.probe?.tool ?? "");
  const [probeArgs, setProbeArgs] = useState(s.probe ? JSON.stringify(s.probe.arguments) : "{}");
  const [confirmWrite, setConfirmWrite] = useState(s.probe?.confirmed_write ?? false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [facts, setFacts] = useState<McpReviewFacts | null>(null);
  const installed = s.installed as { source?: McpSource } | null;
  const needsInstall = s.transport === "stdio" && !(installed && sameSource(installed.source, s.source));

  const probeError = useMemo(() => {
    if (!probeTool) return "";
    try {
      const v = JSON.parse(probeArgs || "{}");
      return v && typeof v === "object" && !Array.isArray(v) ? "" : "Arguments are a JSON object.";
    } catch {
      return "Arguments are a JSON object.";
    }
  }, [probeTool, probeArgs]);

  const save = async () => {
    const errs = validateRows(rows, s.transport, secretsState.names);
    setRowErrors(errs);
    if (Object.keys(errs).length || probeError) return;
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
      const source: McpSource =
        s.source.kind === "remote" ? { kind: "remote", url: url.trim() } : { ...s.source, version: version.trim() };
      await api.updateMcpServer(
        s.id,
        {
          settings: settingsFromRows(stored.rows, s.transport),
          ...(s.transport === "stdio" ? { args: argsFromText(argsText) } : {}),
          probe: probeTool
            ? { tool: probeTool, arguments: JSON.parse(probeArgs || "{}"), confirmed_write: confirmWrite }
            : null,
          ...(sameSource(source, s.source) ? {} : { source }),
        },
        s.updated_at,
      );
      onSaved(
        sameSource(source, s.source)
          ? "Saved. Checks for the old values are stale: run them again before projects receive the server."
          : "Saved the new version. Review and install it, then run the checks again.",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  // Polled operations (#2163 P1b): resolving or installing a package can take minutes.
  const reviewOp = useMcpOperation();
  const installOp = useMcpOperation();
  const review = async () => {
    setBusy(true);
    setError("");
    try {
      const reviewed = await reviewOp(`${s.id}@${s.updated_at}`, (rid) => api.reviewMcpServer(s.id, rid));
      if (reviewed) setFacts(reviewed);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const install = async () => {
    if (!facts) return;
    setBusy(true);
    setError("");
    try {
      const done = await installOp(`${s.id}:${facts.review_id}`, (rid) => api.installMcpServer(s.id, facts.review_id, rid));
      if (!done) return;
      onSaved("Installed. Run the checks again before projects receive the new version.");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.valueCell}>
      {needsInstall && (
        <Panel rail="short" faceClassName={styles.face} data-testid="mcp-install-panel">
          <Callsign title="Install" value={s.source.kind === "remote" ? "" : `${s.source.package} ${s.source.version}`} />
          {!facts ? (
            <>
              <p className={styles.note}>
                {s.source.kind === "go"
                  ? "This version is not built yet. Review what would be built, then install it."
                  : "This version is not installed. Review what would run, then install it."}
              </p>
              <div className={styles.btnRow}>
                <button type="button" className={styles.primary} onClick={() => void review()} disabled={busy}>
                  {busy ? "Reviewing…" : "Review"}
                </button>
              </div>
              {busy && (
                <p className={styles.note} role="status" data-testid="mcp-op-note">
                  {s.source.kind === "go"
                    ? "Checking the module and every dependency against sum.golang.org. On a busy host this can take a minute or two."
                    : "Resolving the exact version into a lock. On a busy host this can take a minute or two."}
                </p>
              )}
            </>
          ) : (
            <>
              <ReviewFacts facts={facts} />
              <Caution title="Third-party code">
                It runs as your user inside every session that gets this server. Review before installing.
              </Caution>
              <div className={styles.btnRow}>
                <button type="button" className={styles.primary} onClick={() => void install()} disabled={busy}>
                  <Package size={14} aria-hidden="true" /> {busy ? "Installing…" : "Install"}
                </button>
              </div>
              {busy && (
                <p className={styles.note} role="status" data-testid="mcp-op-note">
                  {s.source.kind === "go"
                    ? "A build can take a few minutes; it carries on even if you leave this page."
                    : "A package can take a few minutes; the install carries on even if you leave this page."}
                </p>
              )}
            </>
          )}
        </Panel>
      )}
      <Panel rail="short" faceClassName={styles.face}>
        <Callsign title="Settings & credentials" value="shared" />
        <SettingsEditor
          rows={rows}
          onChange={setRows}
          transport={s.transport}
          serverName={s.name}
          secrets={secretsState.secrets}
          errors={rowErrors}
          disabled={busy}
        />
        {s.transport === "stdio" && (
          <Field
            as="textarea"
            label="Arguments"
            mono
            rows={2}
            value={argsText}
            onChange={(e) => setArgsText(e.target.value)}
            hint="Added to the server’s command, one per line. Literal: never run through a shell."
          />
        )}
      </Panel>
      <Panel rail="short" faceClassName={styles.face} data-testid="mcp-probe">
        <Callsign title="Probe call" value={probeTool ? probeTool : "none"} />
        {tools.length === 0 && !probeTool ? (
          <p className={styles.note}>
            Optional: one tool the check calls to prove the server answers. Run a check that passes first; the probe
            is picked from the tools it found.
          </p>
        ) : (
          <div className={styles.probe}>
            <Field as="select" label="Tool" mono value={probeTool} onChange={(e) => setProbeTool(e.target.value)}>
              <option value="">No probe call</option>
              {tools.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
              {probeTool && !tools.includes(probeTool) && <option value={probeTool}>{probeTool} (not in the last check)</option>}
            </Field>
            {probeTool && (
              <>
                <Field
                  as="textarea"
                  label="Arguments (JSON)"
                  mono
                  rows={2}
                  value={probeArgs}
                  onChange={(e) => setProbeArgs(e.target.value)}
                  error={probeError || undefined}
                />
                <label className={styles.confirm}>
                  <input type="checkbox" checked={confirmWrite} onChange={(e) => setConfirmWrite(e.target.checked)} />
                  <span>
                    <b>This call may change data</b> — call it even if the server does not mark the tool read-only.
                  </span>
                </label>
                <p className={styles.note}>
                  The check calls the tool only when the server marks it read-only (<code>readOnlyHint</code>), unless
                  you tick the box above. Pick a harmless read, such as listing a folder.
                </p>
              </>
            )}
          </div>
        )}
      </Panel>
      <Panel rail="short" faceClassName={styles.face} data-testid="mcp-source-panel">
        <Callsign title="Source" value={s.source.kind} />
        {s.origin && <OriginNote origin={s.origin} source={s.source} />}
        {s.source.kind === "remote" ? (
          <Field label="Server URL" mono type="url" value={url} onChange={(e) => setUrl(e.target.value)} hint="https only." />
        ) : s.source.kind === "go" ? null : (
          // A Go module's version is never typed in (#2427): only Update from source changes it.
          <Field
            label={`${s.source.package} version`}
            mono
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            hint={
              s.origin
                ? "A new exact version is reviewed and installed again before it runs. Typing one drops the repository line; Update from source keeps it."
                : "A new exact version is reviewed and installed again before it runs."
            }
          />
        )}
        {s.origin && (
          <div className={styles.btnRow}>
            <Link className={styles.ghost} to={mcpServerUpdatePath(s.id)} data-testid="mcp-update-from-source">
              <GitBranch size={14} aria-hidden="true" /> Update from source
            </Link>
          </div>
        )}
      </Panel>
      <p className={styles.note}>
        Saving a change makes every check stale: the server reaches no new session until a check passes again. Running
        sessions keep what they started with until they exit.
      </p>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
      <div className={styles.btnRow}>
        <button type="button" className={styles.primary} onClick={() => void save()} disabled={busy} data-testid="mcp-settings-save">
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

/** A project's OWN credential values: write-only fields, sent once, never read back. */
function OwnCredentialsDialog({
  serverId,
  row,
  variables,
  projectName,
  onClose,
}: {
  serverId: string;
  row: McpProjectStatus;
  variables: string[];
  projectName: string;
  onClose: (saved: boolean) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const ok = variables.every((v) => (values[v] ?? "").length >= SECRET_MIN && values[v] === values[v].trim());
  return (
    <ConfirmDialog
      tag={row.credentials === "own" ? "Edit own credentials" : "Own credentials"}
      title={`${projectName}: its own credentials`}
      confirmLabel={busy ? "Saving…" : "Save"}
      confirmDisabled={!ok || busy}
      busy={busy}
      onCancel={() => onClose(false)}
      onConfirm={() => {
        setBusy(true);
        setError("");
        api.setMcpOwnCredentials(serverId, row.project_id, values).then(
          () => onClose(true),
          (e: unknown) => {
            setError(errorText(e));
            setBusy(false);
          },
        );
      }}
    >
      <div className={styles.ownForm}>
        <p className={styles.note}>
          Stored in Variables for {projectName} only, and used instead of the shared value. Write-only: never shown
          again. Agents in the project can’t read it unless you turn on its agent access in Variables.
        </p>
        {variables.map((v) => (
          <Field
            key={v}
            label={v}
            mono
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={values[v] ?? ""}
            onChange={(e) => setValues((prev) => ({ ...prev, [v]: e.target.value }))}
            hint={`At least ${SECRET_MIN} characters.`}
          />
        ))}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}
      </div>
    </ConfirmDialog>
  );
}
