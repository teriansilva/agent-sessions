/** Library → MCP servers (#2163 P1c, the index board): every registered third-party MCP server with
 *  its source, tool count, projects and state. One row is one link — the whole plate opens the
 *  server's page. Only the status column is coloured, and red only for a check that actually
 *  failed. A failed read says so and offers Retry; it never shows the empty state. */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, Plug, Plus, RefreshCw } from "lucide-react";

import { api } from "../lib/api";
import { MCP_NEW_PATH, mcpServerPath } from "../lib/routes";
import type { McpServerSummary } from "../types/mcp";
import { Loader } from "../components/hud/Loader";
import { ProjectSquares, StateWords } from "../components/mcp/McpBits";
import { errorText, useProjects } from "../components/mcp/mcpData";
import { projectsSummary, sourceLine, stateView } from "../components/mcp/mcpModel";
import styles from "../components/mcp/mcp.module.css";

export default function McpServers() {
  const [servers, setServers] = useState<McpServerSummary[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const { projects, name } = useProjects();

  const load = useCallback(
    () =>
      api.mcpServers().then(
      (r) => {
        setServers(r.servers);
        setError("");
        setLoading(false);
      },
      (e: unknown) => {
        setError(errorText(e));
        setLoading(false);
      },
    ),
    [],
  );
  const retry = () => {
    setLoading(true);
    void load();
  };
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className={styles.page} data-testid="mcp-index">
      <div className={styles.wrap}>
        <div className={styles.headRow}>
          <div>
            <p className={styles.kicker}>
              <span className="nj-notch" aria-hidden="true" />
              Library <span aria-hidden="true">//</span> <b>MCP servers</b>
              {servers && !error && (
                <>
                  <span aria-hidden="true">//</span> {servers.length}
                </>
              )}
            </p>
            <h1 className={styles.h1}>MCP servers</h1>
            <p className={styles.lede}>
              Register a third-party MCP server once. Every agent session in a project you enable gets its tools
              when it starts. Credentials stay in Variables.
            </p>
          </div>
          <Link className={styles.primary} to={MCP_NEW_PATH} data-testid="mcp-add">
            <Plus size={14} aria-hidden="true" />
            Add MCP server
          </Link>
        </div>

        {error ? (
          <div className={styles.alert} role="alert" data-testid="mcp-read-error">
            <h2>Couldn’t load MCP servers</h2>
            <p>
              {error}. Nothing was changed, and sessions keep the servers they started with.
            </p>
            <button type="button" className={styles.ghost} onClick={retry} disabled={loading}>
              <RefreshCw size={14} aria-hidden="true" />
              {loading ? "Retrying…" : "Retry"}
            </button>
          </div>
        ) : servers === null ? (
          <Loader kind="skeleton" label="Acquiring MCP servers" rows={3} />
        ) : servers.length === 0 ? (
          <div className={styles.empty} data-testid="mcp-empty">
            <h2>No MCP servers yet</h2>
            <p className={styles.note}>
              Add one: a remote server by its URL, or an npm or PyPI package at an exact version. BattleLab checks
              it works before any project gets it.
            </p>
            <div className={styles.btnRow}>
              <Link className={styles.primary} to={MCP_NEW_PATH}>
                <Plus size={14} aria-hidden="true" />
                Add MCP server
              </Link>
            </div>
          </div>
        ) : (
          <>
            <div className={styles.listHead} aria-hidden="true">
              <span>Server</span>
              <span>Source</span>
              <span className={styles.num}>Tools</span>
              <span>Projects</span>
              <span>Status</span>
              <span />
            </div>
            <ul className={styles.list} aria-label="MCP servers">
              {servers.map((s) => {
                const src = sourceLine(s);
                const st = stateView(s.state, name);
                const on = (id: string) => s.projects === "all" || s.enabled_projects.includes(id);
                return (
                  <li key={s.id}>
                    <Link className={styles.row} to={mcpServerPath(s.id)} data-testid="mcp-row" data-server={s.name}>
                      <span className={styles.who}>
                        <span className={styles.mark} aria-hidden="true">
                          <Plug size={16} />
                        </span>
                        <span className={styles.name}>
                          <b>{s.label || s.name}</b>
                          <span>{s.name}</span>
                        </span>
                      </span>
                      <span className={styles.src}>
                        <span className={styles.srcMain}>
                          {src.kind} · {src.main}
                        </span>
                        <span className={styles.srcSub}>{src.sub}</span>
                      </span>
                      <span className={styles.tools}>
                        {s.tool_count}
                        <span>tools</span>
                      </span>
                      <span className={styles.projs}>
                        <ProjectSquares projects={projects ?? []} on={on} />
                        {projectsSummary(s)}
                      </span>
                      <span className={styles.rowState}>
                        <StateWords tone={st.tone} words={st.words} testId="mcp-row-state" />
                      </span>
                      <ChevronRight size={16} className={styles.chev} aria-hidden="true" />
                    </Link>
                  </li>
                );
              })}
            </ul>
            <p className={styles.note}>
              Agents see a server’s tools under its name, e.g. <code>nextcloud</code> · <code>list_directory</code>.
              Running sessions pick up a change when they next start.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
