import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { ApiError, api } from "../../lib/api";
import {
  PLAYBOOKS_PATH,
  playbookPath,
  playbookEditPath,
} from "../../lib/routes";
import type {
  PlaybookDetail,
  PlaybookWriteResult,
} from "../../types/playbooks";
import { PlaybookDialog } from "./PlaybookDialog";
import { PlaybookSource } from "./PlaybookCard";
import { PlaybookFlowPreview } from "./PlaybookFlowPreview";
import { flowPath, type Step } from "./playbookDraft";
import { PlaybookFleetView } from "./PlaybookFleet";
import { errorText, usePlaybookRead } from "./usePlaybooks";
import buttons from "../ui/actionButton.module.css";
import styles from "./playbooks.module.css";

function warnings(result: PlaybookWriteResult) {
  return result.durable === false ||
    result.state_durable === false ||
    result.recovery_durable === false
    ? " The change was published, but durable storage or recovery could not be confirmed. Reload and inspect the store before further changes."
    : "";
}

export function PlaybookDetailPage({ id }: { id: string }) {
  const navigate = useNavigate();
  const detail = usePlaybookRead(useCallback(() => api.playbook(id), [id]));
  const gallery = usePlaybookRead(useCallback(() => api.playbooks(), []));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [deleting, setDeleting] = useState<HTMLElement | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  const pb = detail.data;

  async function reload() {
    const [fresh, list] = await Promise.all([
      detail.reload(),
      gallery.reload(),
    ]);
    if (live.current && fresh && list) {
      setConflict(false);
      setError("");
      setDeleteError("");
    }
  }
  async function act(kind: "duplicate" | "default" | "delete") {
    if (!pb?.revision || busy || conflict) return;
    setBusy(true);
    setError("");
    setDeleteError("");
    setNotice("");
    try {
      if (kind === "duplicate") {
        const copy = await api.duplicatePlaybook(id, pb.revision);
        if (live.current) {
          if (warnings(copy)) {
            setNotice(`Created ${copy.name || copy.id}.${warnings(copy)}`);
            await reload();
          } else navigate(playbookPath(copy.id));
        }
      } else if (kind === "delete") {
        const result = await api.deletePlaybook(id, pb.revision);
        if (live.current) {
          setDeleting(null);
          if (warnings(result)) {
            setNotice(`Deleted ${pb.name || id}.${warnings(result)}`);
            await reload();
          } else navigate(PLAYBOOKS_PATH);
        }
      } else {
        if (!gallery.data || gallery.error || gallery.loading) return;
        const result =
          gallery.data.default === id
            ? await api.clearDefaultPlaybook(id)
            : await api.setDefaultPlaybook(
                id,
                pb.revision,
                gallery.data.default,
              );
        if (live.current) {
          setNotice(`Default updated.${warnings(result)}`);
          await reload();
        }
      }
    } catch (e) {
      if (!live.current) return;
      let text = errorText(e);
      const record =
        e instanceof ApiError
          ? (e.record as
              { projects?: { id: string; name?: string }[] } | undefined)
          : undefined;
      if (record?.projects?.length)
        text += ` Remove deployments first: ${record.projects.map((p) => p.name || p.id).join(", ")}.`;
      if (kind === "delete") setDeleteError(text);
      else setError(text);
      if (e instanceof ApiError && e.status === 409) setConflict(true);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  return (
    <>
      <Link className={styles.back} to={PLAYBOOKS_PATH}>
        ← All playbooks
      </Link>
      {detail.error && (
        <div role="alert" className={styles.error}>
          {detail.error}
          {pb ? " Showing the last successful read." : ""}
          <button className={buttons.ghost} onClick={() => void reload()}>
            Reload current playbooks
          </button>
        </div>
      )}
      {!pb && detail.loading && <p role="status">Loading playbook…</p>}
      {pb && (
        <>
          <div className={styles.header}>
            <div>
              <div className={styles.kicker}>Library // playbooks</div>
              <h1>{pb.name || pb.id}</h1>
              <PlaybookSource card={pb} />
            </div>
            <div className={styles.actions}>
              {pb.ok && pb.editable && (
                <Link className={buttons.primary} to={playbookEditPath(id)}>
                  Edit playbook
                </Link>
              )}
              {pb.ok && (
                <button
                  className={buttons.ghost}
                  disabled={busy || conflict || !pb.revision}
                  onClick={() => void act("duplicate")}
                >
                  {pb.editable ? "Duplicate" : "Duplicate to local"}
                </button>
              )}
              {pb.ok && (
                <button
                  className={buttons.ghost}
                  disabled={
                    busy ||
                    conflict ||
                    gallery.loading ||
                    !!gallery.error ||
                    !gallery.data
                  }
                  onClick={() => void act("default")}
                >
                  {gallery.data?.default === id
                    ? "Clear default"
                    : "Set as default"}
                </button>
              )}
              {pb.editable && (
                <button
                  className={buttons.ghost}
                  disabled={busy || conflict || !pb.revision}
                  onClick={(e) => setDeleting(e.currentTarget)}
                >
                  Delete playbook…
                </button>
              )}
            </div>
          </div>
          {notice && <p role="status">{notice}</p>}
          {(error || gallery.error) && (
            <div role="alert" className={styles.error}>
              {error || gallery.error}
              <button
                className={buttons.ghost}
                disabled={busy || detail.loading || gallery.loading}
                onClick={() => void reload()}
              >
                Reload current playbooks
              </button>
            </div>
          )}
          <p className={styles.intro}>{pb.summary}</p>
          {!pb.ok ? (
            <p role="alert" className={styles.error}>
              {pb.error}
            </p>
          ) : (
            <>
              <div className={styles.detailGrid}>
                <div>
                  {(pb.flows ?? []).map((flow) => (
                    <section key={flow.id} className={styles.section}>
                      <h2>Flow // {flow.title}</h2>
                      <PlaybookFlowPreview
                        steps={
                          (pb.documents?.[flowPath(flow.id)]?.steps ??
                            flow.steps) as Step[]
                        }
                      />
                    </section>
                  ))}
                  {!pb.flows?.length && <p>No flow defined.</p>}
                  <section className={styles.section}>
                    <h2>Instructions</h2>
                    <div className={styles.markdown}>
                      {pb.readme ? (
                        <ReactMarkdown
                          skipHtml
                          components={{
                            h1: ({ children }) => <h3>{children}</h3>,
                            h2: ({ children }) => <h3>{children}</h3>,
                          }}
                          allowedElements={[
                            "p",
                            "h1",
                            "h2",
                            "h3",
                            "h4",
                            "ul",
                            "ol",
                            "li",
                            "strong",
                            "em",
                            "code",
                            "pre",
                            "blockquote",
                            "br",
                            "hr",
                          ]}
                          unwrapDisallowed
                        >
                          {pb.readme}
                        </ReactMarkdown>
                      ) : (
                        <p>No README provided.</p>
                      )}
                    </div>
                    {Object.entries(pb.files ?? {})
                      .filter(
                        ([p]) =>
                          p.startsWith("template/") ||
                          p.startsWith("runbooks/"),
                      )
                      .map(([path, content]) => (
                        <details key={path}>
                          <summary>{path}</summary>
                          {typeof content === "string" ? (
                            <pre>{content}</pre>
                          ) : (
                            <p>Binary file · preview unavailable.</p>
                          )}
                        </details>
                      ))}
                  </section>
                </div>
                <aside>
                  <BundleFacts pb={pb} />
                </aside>
              </div>
              <PlaybookFleetView key={`${pb.id}:${pb.revision}`} id={id} />
            </>
          )}
          {!!pb.recovery_total && (
            <p>
              {pb.recovery_total} previous or interrupted copies are retained
              for recovery.
            </p>
          )}
        </>
      )}
      {deleting && pb && (
        <PlaybookDialog
          tag="Delete playbook"
          title={`Delete ${pb.name || id}?`}
          danger
          busy={busy || detail.loading || gallery.loading}
          returnFocusTo={deleting}
          confirmLabel={conflict ? "Reload before deleting" : "Delete playbook"}
          onCancel={() => {
            setDeleting(null);
            if (conflict) void reload();
          }}
          onConfirm={() => (conflict ? void reload() : void act("delete"))}
        >
          <p>
            Projects must stop using this playbook before it can be deleted. If
            it is the default, deletion clears that choice. A recovery copy is
            retained.
          </p>
          {deleteError && (
            <p role="alert" className={styles.error}>
              {deleteError}
            </p>
          )}
        </PlaybookDialog>
      )}
    </>
  );
}

function BundleFacts({ pb }: { pb: PlaybookDetail }) {
  const manifest = pb.documents?.["playbook.toml"] ?? {};
  const variables = (manifest.variables ?? []) as {
    name: string;
    kind?: string;
    type?: string;
    required?: boolean;
    default?: unknown;
  }[];
  const materials = (manifest.materials ?? []) as {
    path: string;
    disposition?: string;
    kind?: string;
    target?: string;
  }[];
  return (
    <>
      <section className={styles.section}>
        <h2>Variables // {variables.length}</h2>
        {variables.length ? (
          <dl>
            {variables.map((v) => (
              <div key={v.name}>
                <dt>
                  <code>{`{{${v.name}}}`}</code>
                </dt>
                <dd>
                  {v.kind === "secret"
                    ? "Secret · value never shown"
                    : `${v.type || "text"} · ${v.required ? "required" : "optional"}`}
                  {v.kind !== "secret" && v.default !== undefined && (
                    <pre>Default: {String(v.default)}</pre>
                  )}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p>No variables.</p>
        )}
      </section>
      <section className={styles.section}>
        <h2>Ships</h2>
        {materials.length ? (
          <ul>
            {materials.map((m) => (
              <li key={m.path}>
                <code>{m.path}</code> ·{" "}
                {m.kind === "instruction-alias"
                  ? `alias → ${m.target}`
                  : m.disposition}
              </li>
            ))}
          </ul>
        ) : (
          <p>No material files.</p>
        )}
        {Object.entries(pb.files ?? {})
          .filter(([path]) => path.startsWith("templates/"))
          .map(([path, contents]) => (
            <details key={path}>
              <summary>{path}</summary>
              <pre>
                {typeof contents === "string" ? contents : "Binary file"}
              </pre>
            </details>
          ))}
      </section>
      <section className={styles.section}>
        <h2>Requires</h2>
        {Object.entries(pb.requires_present?.binaries ?? {}).map(
          ([name, present]) => (
            <p key={name}>
              <code>{name}</code>{" "}
              <span className={present ? styles.good : styles.warn}>
                {present ? "present" : "missing"}
              </span>
            </p>
          ),
        )}
        <p>Connections: {pb.connections?.join(", ") || "none"}</p>
        <p>Capabilities requested: {pb.capabilities?.join(", ") || "none"}</p>
        <p>
          Step assignments are resolved in the deployment review. Stored agent
          and model references are preserved.
        </p>
      </section>
    </>
  );
}
