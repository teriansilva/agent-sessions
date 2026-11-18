import { FileRow, Origin } from "../gallery/Shared";
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
import { playbookProvenance, playbookSource } from "./playbookSource";
import { ArrowLeft, Copy, Pencil, Star, Trash2 } from "lucide-react";
import type { RowMenuEntry } from "../sidebar/RowMenu";
import { DefaultMark, type LibrarySource } from "../library/LibraryCard";
import {
  ActionBar,
  DetailSection,
  Fact,
  LibraryDetail,
  MoreMenu,
  SourceFact,
} from "../library/LibraryDetail";
import lib from "../library/library.module.css";
import { useExportEntry, useImportEntry } from "../share/useLibraryShare";
import { useShareImports } from "../share/useShareImports";
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
  const [round, setRound] = useState(0);
  const imported = useShareImports(round);
  // Import… here makes NEW copies only, so this playbook is unchanged; closing results that
  // created something re-reads the list behind it (the gallery shows the copy without a reload).
  const importer = useImportEntry(() => {
    setRound((n) => n + 1);
    void gallery.reload();
  });
  const exporter = useExportEntry("playbook");

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
  const source = pb ? playbookSource(pb, imported.has(`playbook:${pb.id}`)) : "local";
  const isDefault = gallery.data ? gallery.data.default === id : !!pb?.default;
  const reloadAlert = detail.error && (
    <div role="alert" className={lib.error}>
      {detail.error}
      {pb ? " Showing the last successful read." : ""}
      <button className={buttons.ghost} onClick={() => void reload()}>
        Reload current playbooks
      </button>
    </div>
  );
  return (
    <>
      {!pb && (
        <>
          <Link className={lib.back} to={PLAYBOOKS_PATH}>
            <ArrowLeft size={16} aria-hidden />
            All playbooks
          </Link>
          {reloadAlert}
          {detail.loading && <p role="status">Loading playbook…</p>}
        </>
      )}
      {pb && (
        <LibraryDetail
          testId="playbook-detail"
          back={{ to: PLAYBOOKS_PATH, label: "All playbooks" }}
          kicker="playbooks"
          kind="Playbook"
          version={pb.version}
          source={source}
          badges={isDefault ? <DefaultMark /> : undefined}
          title={pb.name || pb.id}
          bar={
            <ActionBar
              primary={
                pb.ok && pb.editable ? (
                  <Link className={buttons.primary} to={playbookEditPath(id)}>
                    <Pencil size={16} aria-hidden />
                    Edit playbook
                  </Link>
                ) : pb.ok ? (
                  <button
                    type="button"
                    className={buttons.primary}
                    disabled={busy || conflict || !pb.revision}
                    onClick={() => void act("duplicate")}
                  >
                    <Copy size={16} aria-hidden />
                    Duplicate to local
                  </button>
                ) : undefined
              }
              onExport={pb.ok && pb.source === "local" ? () => exporter.open(id) : undefined}
              onImport={importer.open}
            >
              <PlaybookMenu
                title={pb.name || pb.id}
                ok={pb.ok}
                editable={pb.editable}
                isDefault={isDefault}
                writeHeld={busy || conflict || !pb.revision}
                defaultHeld={
                  busy || conflict || gallery.loading || !!gallery.error || !gallery.data
                }
                onAction={(kind, from) =>
                  kind === "delete" ? setDeleting(from) : void act(kind)
                }
              />
            </ActionBar>
          }
          notices={
            <>
              {reloadAlert}
              {notice && <p role="status">{notice}</p>}
              {(error || gallery.error) && (
                <div role="alert" className={lib.error}>
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
            </>
          }
          lead={pb.summary}
          facts={<BundleFacts pb={pb} source={source} isDefault={isDefault} />}
        >
          <Origin value={pb.gallery_origin} kind="playbook" id={pb.id} revision={pb.revision} />
          {!pb.ok ? (
            <p role="alert" className={lib.error}>
              {pb.error}
            </p>
          ) : (
            <>
              {(pb.flows ?? []).map((flow) => (
                <DetailSection key={flow.id} title={`Flow // ${flow.title}`}>
                  <PlaybookFlowPreview
                    steps={(pb.documents?.[flowPath(flow.id)]?.steps ?? flow.steps) as Step[]}
                  />
                </DetailSection>
              ))}
              {!pb.flows?.length && <p>No flow defined.</p>}
              <p className={lib.intro}>
                Format 5 steps name roles, filled per mission by a specialist from your pool.
                Older steps keep their stored agent and model, and their assignments are
                resolved in the deployment review.
              </p>
              <DetailSection title="Instructions">
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
              </DetailSection>
              <BundleFiles pb={pb} />
              <PlaybookFleetView key={`${pb.id}:${pb.revision}`} id={id} />
            </>
          )}
        </LibraryDetail>
      )}
      {importer.dialog}
      {exporter.dialog}
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

/** The ⋯ menu: what the action bar doesn't lead with (#2144). Only actions that exist today. */
function PlaybookMenu({
  title,
  ok,
  editable,
  isDefault,
  writeHeld,
  defaultHeld,
  onAction,
}: {
  title: string;
  ok: boolean;
  editable: boolean;
  isDefault: boolean;
  writeHeld: boolean;
  defaultHeld: boolean;
  /** `from` is the ⋯ trigger, for the delete confirm to hand focus back to. */
  onAction: (kind: "duplicate" | "default" | "delete", from: HTMLElement) => void;
}) {
  const trigger = () =>
    document.querySelector<HTMLElement>("[data-testid='library-more']") ?? document.body;
  const items: RowMenuEntry[] = [];
  if (ok && editable)
    items.push({
      key: "duplicate",
      label: "Duplicate",
      icon: <Copy size={16} aria-hidden />,
      disabled: writeHeld,
      onSelect: () => onAction("duplicate", trigger()),
    });
  if (ok)
    items.push({
      key: "default",
      label: isDefault ? "Clear default" : "Set as default",
      icon: <Star size={16} aria-hidden />,
      disabled: defaultHeld,
      onSelect: () => onAction("default", trigger()),
    });
  if (editable)
    items.push("separator", {
      key: "delete",
      label: "Delete playbook…",
      icon: <Trash2 size={16} aria-hidden />,
      danger: true,
      disabled: writeHeld,
      onSelect: () => onAction("delete", trigger()),
    });
  return <MoreMenu items={items} title={title} />;
}

/** The files a playbook ships, each a disclosure with its text (the README is Instructions). */
function BundleFiles({ pb }: { pb: PlaybookDetail }) {
  const files = Object.entries(pb.files ?? {}).filter(
    ([p]) => p.startsWith("template/") || p.startsWith("templates/") || p.startsWith("runbooks/"),
  );
  if (!files.length) return null;
  return (
    <DetailSection title={`Files // ${files.length}`}>
      {files.map(([path, content]) => (
        <FileRow key={path} name={path}>
          {typeof content === "string" ? <pre>{content}</pre> : <p>Binary file · preview unavailable.</p>}
        </FileRow>
      ))}
    </DetailSection>
  );
}

/** The quiet side panel: version, source, requirements, variables and what it ships. */
function BundleFacts({
  pb,
  source,
  isDefault,
}: {
  pb: PlaybookDetail;
  source: LibrarySource;
  isDefault: boolean;
}) {
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
      <Fact label="Version">
        {pb.version ? `v${pb.version.replace(/^v/i, "")}` : "Unversioned"}
        <span className={lib.small}>
          {pb.revision ? `revision ${pb.revision.slice(0, 7)}` : "revision unknown"}
          {pb.recovery_total ? ` · ${pb.recovery_total} retained for recovery` : ""}
        </span>
      </Fact>
      <SourceFact
        word={playbookProvenance(pb, source)}
        note={isDefault ? "Default for new projects." : undefined}
      />
      <Fact label="Requires">
        {Object.entries(pb.requires_present?.binaries ?? {}).map(([name, present]) => (
          <p key={name}>
            <code>{name}</code>{" "}
            <span className={present ? lib.good : lib.warn}>{present ? "present" : "missing"}</span>
          </p>
        ))}
        <span className={lib.small}>
          Connections: {pb.connections?.join(", ") || "none"} · capabilities:{" "}
          {pb.capabilities?.join(", ") || "none"}
        </span>
      </Fact>
      <Fact label={`Variables · ${variables.length}`}>
        {variables.length ? (
          variables.map((v) => (
            <p key={v.name}>
              <code>{`{{${v.name}}}`}</code>{" "}
              {v.kind === "secret"
                ? "Secret · value never shown"
                : `${v.type || "text"} · ${v.required ? "required" : "optional"}`}
              {v.kind !== "secret" && v.default !== undefined && (
                <span className={lib.small}>Default: {String(v.default)}</span>
              )}
            </p>
          ))
        ) : (
          <span className={lib.small}>No variables.</span>
        )}
      </Fact>
      <Fact label="Ships">
        {materials.length ? (
          materials.map((m) => (
            <p key={m.path}>
              <code>{m.path}</code> ·{" "}
              {m.kind === "instruction-alias" ? `alias → ${m.target}` : m.disposition}
            </p>
          ))
        ) : (
          <span className={lib.small}>No material files.</span>
        )}
      </Fact>
    </>
  );
}
