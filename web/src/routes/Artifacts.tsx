/** Library → Artifacts (#1932 P2, board 1): every file and static site the agents published, newest
 *  first, with what it is, who made it (agent, session, project, mission), its latest revision and
 *  size, and whether it is PRIVATE or PUBLIC — the one list to audit and revoke public links.
 *
 *  The filters live in the URL (`?q=&project=&session=&mission=&kind=&visibility=`), so a filtered
 *  view is a link (Settings → Artifacts links `?visibility=public`). The server filters and pages;
 *  this page never filters a loaded page itself. On a phone search and the two segments stay
 *  inline, and project, session and mission sit behind one button (decision 10). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  Copy,
  ExternalLink,
  Folder,
  MoreHorizontal,
  Search,
  SlidersHorizontal,
  Crosshair,
  X,
  Power,
  Ban,
  Trash2,
  FileText,
} from "lucide-react";

import { ApiError, api } from "../lib/api";
import { useIsMobile } from "../lib/useIsMobile";
import { artifactPath, hostedArtifactPath } from "../lib/routes";
import { appUrl } from "../lib/appBase";
import { settingsPath } from "./settingsTabs";
import type {
  Artifact,
  ArtifactKind,
  ArtifactPage,
  ArtifactSettings,
  ArtifactVisibility,
} from "../types/artifacts";
import { Callsign, Panel } from "../components/hud/Panel";
import { Chip, Seg } from "../components/hud/Controls";
import { Loader } from "../components/hud/Loader";
import { Readout } from "../components/hud/Meters";
import { RowMenu, type RowMenuEntry } from "../components/sidebar/RowMenu";
import { ConfirmDialog } from "../components/templates/ConfirmDialog";
import { KindTag, VisibilityTag } from "../components/artifacts/ArtifactBits";
import { StoreGauge } from "../components/artifacts/StoreGauge";
import { useArtifactNames } from "../components/artifacts/useArtifactNames";
import {
  absoluteLink,
  ago,
  filesLabel,
  formatSize,
  revLabel,
} from "../components/artifacts/artifactModel";
import styles from "../components/artifacts/artifacts.module.css";

type KindF = "all" | ArtifactKind;
type VisF = "all" | ArtifactVisibility;
const message = (e: unknown) =>
  e instanceof ApiError || e instanceof Error
    ? e.message
    : "The request failed. Try again.";

export default function Artifacts() {
  const mobile = useIsMobile();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const project = params.get("project") ?? "";
  const session = params.get("session") ?? "";
  const mission = params.get("mission") ?? "";
  const kind = (params.get("kind") as KindF | null) ?? "all";
  const vis = (params.get("visibility") as VisF | null) ?? "all";
  const qParam = params.get("q") ?? "";
  const [q, setQ] = useState(qParam);
  const [page, setPage] = useState<ArtifactPage | null>(null);
  const [settings, setSettings] = useState<ArtifactSettings | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [moreOpen, setMoreOpen] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [confirm, setConfirm] = useState<{
    kind: "revoke" | "delete";
    a: Artifact;
    returnTo: HTMLElement | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);

  const setParam = useCallback(
    (key: string, value: string) =>
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value && value !== "all") next.set(key, value);
          else next.delete(key);
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );

  // The search box writes the URL a beat after typing stops.
  useEffect(() => {
    if (q === qParam) return;
    const t = window.setTimeout(() => setParam("q", q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q, qParam, setParam]);

  const query = useMemo(
    () => ({
      project: project || undefined,
      session: session || undefined,
      mission: mission || undefined,
      kind: kind === "all" ? undefined : kind,
      visibility: vis === "all" ? undefined : vis,
      q: qParam || undefined,
    }),
    [project, session, mission, kind, vis, qParam],
  );

  const load = useCallback(() => {
    const gen = ++generation.current;
    return api.artifacts(query).then(
      (next) => {
        if (gen !== generation.current) return;
        setPage(next);
        setError("");
      },
      (e: unknown) => {
        if (gen === generation.current) setError(message(e));
      },
    );
  }, [query]);
  useEffect(() => {
    load().catch(() => {});
  }, [load]);
  const loadSettings = useCallback(
    () =>
      api
        .artifactSettings()
        .then(setSettings)
        .catch(() => {}),
    [],
  );
  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const loadMore = async () => {
    if (!page?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    const gen = generation.current;
    try {
      const next = await api.artifacts({ ...query, cursor: page.next_cursor });
      if (gen === generation.current)
        setPage({ ...next, items: [...page.items, ...next.items] });
    } catch (e) {
      setError(message(e));
    } finally {
      setLoadingMore(false);
    }
  };

  const items = useMemo(() => page?.items ?? [], [page]);
  const names = useArtifactNames(
    useMemo(() => items.map((a) => a.origin.session_key), [items]),
  );

  const copy = async (a: Artifact) => {
    try {
      await navigator.clipboard.writeText(
        absoluteLink(a.public_url ?? a.url, window.location.origin),
      );
      setStatus(`Link to “${a.title}” copied.`);
    } catch {
      setStatus(
        "Copying needs a secure origin; open the artifact to see its link.",
      );
    }
  };

  const act = async () => {
    if (!confirm || busy) return;
    setBusy(true);
    try {
      if (confirm.kind === "revoke") {
        await api.revokeArtifact(confirm.a.id);
        setStatus(`The public link to “${confirm.a.title}” is ended.`);
      } else {
        await api.deleteArtifact(confirm.a.id);
        setStatus(`“${confirm.a.title}” is deleted.`);
      }
      setConfirm(null);
      await Promise.all([load(), loadSettings()]);
    } catch (e) {
      setStatus(message(e));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  };

  const filtered = !!(
    project ||
    session ||
    mission ||
    qParam ||
    kind !== "all" ||
    vis !== "all"
  );
  const clearAll = () => {
    setQ("");
    setParams(new URLSearchParams(), { replace: true });
  };
  const hiddenActive = [project, session, mission].filter(Boolean).length;

  const chips: { key: string; label: string; clear: () => void }[] = [];
  if (vis !== "all")
    chips.push({
      key: "visibility",
      label: vis,
      clear: () => setParam("visibility", ""),
    });
  if (kind !== "all")
    chips.push({
      key: "kind",
      label: kind === "site" ? "Sites" : "Files",
      clear: () => setParam("kind", ""),
    });
  if (project)
    chips.push({
      key: "project",
      label: names.project(project) ?? project,
      clear: () => setParam("project", ""),
    });
  if (session)
    chips.push({
      key: "session",
      label: names.session(session) ?? session,
      clear: () => setParam("session", ""),
    });
  if (mission)
    chips.push({
      key: "mission",
      label: names.mission(mission) ?? mission,
      clear: () => setParam("mission", ""),
    });
  if (qParam)
    chips.push({ key: "q", label: `“${qParam}”`, clear: () => setQ("") });

  const sessionOptions = useMemo(() => {
    const keys = [...new Set(items.map((a) => a.origin.session_key))];
    if (session && !keys.includes(session)) keys.unshift(session);
    return keys;
  }, [items, session]);

  const selects = (
    <>
      <select
        className={`nj-field ${styles.select}`}
        aria-label="Project"
        value={project}
        onChange={(e) => setParam("project", e.target.value)}
      >
        <option value="">All projects</option>
        {names.projects.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
        {project && !names.projects.some((p) => p.id === project) && (
          <option value={project}>{project}</option>
        )}
      </select>
      <select
        className={`nj-field ${styles.select}`}
        aria-label="Session"
        value={session}
        onChange={(e) => setParam("session", e.target.value)}
      >
        <option value="">All sessions</option>
        {sessionOptions.map((k) => (
          <option key={k} value={k}>
            {names.session(k) ?? k}
          </option>
        ))}
      </select>
      <select
        className={`nj-field ${styles.select}`}
        aria-label="Mission"
        value={mission}
        onChange={(e) => setParam("mission", e.target.value)}
      >
        <option value="">All missions</option>
        {names.missions.map((m) => (
          <option key={m.id} value={m.id}>
            {m.title}
          </option>
        ))}
        {mission && !names.missions.some((m) => m.id === mission) && (
          <option value={mission}>{mission}</option>
        )}
      </select>
    </>
  );

  const rowMenu = (a: Artifact): RowMenuEntry[] => [
    {
      key: "details",
      label: "Details",
      icon: <FileText size={15} />,
      onSelect: () => navigate(artifactPath(a.id)),
    },
    ...(a.visibility === "public"
      ? [
          {
            key: "revoke",
            label: "Revoke public link",
            icon: <Ban size={15} />,
            danger: true,
            onSelect: () =>
              setConfirm({
                kind: "revoke",
                a,
                returnTo: document.activeElement as HTMLElement | null,
              }),
          },
        ]
      : []),
    {
      key: "delete",
      label: "Delete…",
      icon: <Trash2 size={15} />,
      danger: true,
      onSelect: () =>
        setConfirm({
          kind: "delete",
          a,
          returnTo: document.activeElement as HTMLElement | null,
        }),
    },
  ];

  const enabled = settings?.enabled;
  const nothingAtAll = page !== null && page.total === 0 && !filtered;

  return (
    <div className={styles.page} data-testid="artifacts-page">
      <div className={styles.wrap}>
        <div>
          <p className={styles.kicker}>
            <span className="nj-notch" aria-hidden="true" />
            Library <span aria-hidden="true">//</span> <b>Artifacts</b>
          </p>
          <h1 className={styles.h1}>Artifacts</h1>
        </div>
        <div className={styles.headRow}>
          <p className={styles.lede}>
            Files and static sites your agents published. Each one is a frozen
            copy at its own link, private to you until you share it.
          </p>
          {settings && (
            <div className={styles.totals} data-testid="artifacts-totals">
              <div className={styles.total}>
                <Readout value={settings.artifacts} small />
                <span className={styles.cap}>artifacts</span>
              </div>
              <div className={styles.total}>
                <Readout value={settings.public} small />
                <span className={styles.cap}>public links</span>
              </div>
              <div className={styles.total}>
                <StoreGauge
                  bytes={settings.bytes}
                  total={settings.caps.total_bytes}
                />
                <span className={styles.cap}>
                  {formatSize(settings.bytes)} of{" "}
                  {formatSize(settings.caps.total_bytes)}
                </span>
              </div>
            </div>
          )}
        </div>

        {nothingAtAll && enabled === false ? (
          <Panel
            faceClassName={styles.face}
            tone="quiet"
            rail="short"
            className={styles.empty}
            data-testid="artifacts-off"
          >
            <Callsign title="Artifacts" value="off" />
            <h3>Artifacts are off</h3>
            <p className={styles.note}>
              Turn them on and your agents can host what they make on this
              BattleLab, each at its own stable link.
            </p>
            <ul className={styles.bullets}>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Publish:</b> an agent copies a file or a static site into
                  BattleLab. Later edits change nothing until it publishes
                  again.
                </span>
              </li>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Private by default:</b> only you, signed in, can open a
                  link.
                </span>
              </li>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Share when you choose:</b> make one public, and revoke it
                  again at any time.
                </span>
              </li>
            </ul>
            <div className={styles.btnRow}>
              <Link className={styles.primary} to={settingsPath("artifacts")}>
                <Power size={14} aria-hidden="true" />
                Turn on in Settings
              </Link>
            </div>
          </Panel>
        ) : nothingAtAll ? (
          <Panel
            faceClassName={styles.face}
            tone="quiet"
            rail="none"
            className={styles.empty}
            data-testid="artifacts-none"
          >
            <Callsign title="Artifacts" value="none yet" />
            <h3>Nothing published yet</h3>
            <ul className={styles.bullets}>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Ask for one:</b> “Publish the load-test report as an
                  artifact.” The agent answers with the link.
                </span>
              </li>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Turn the tool on:</b> a session&apos;s ⋯ menu → Artifacts,
                  a mission&apos;s ⋯ menu, or a project&apos;s session defaults
                  in Settings → Projects.
                </span>
              </li>
              <li>
                <span className={styles.sq} aria-hidden="true" />
                <span>
                  <b>Find them later:</b> here, in each session&apos;s Artifacts
                  tab, or ask HQ.
                </span>
              </li>
            </ul>
          </Panel>
        ) : (
          <>
            <div
              className={styles.filters}
              role="search"
              data-testid="artifacts-filters"
            >
              <label className={styles.search}>
                <Search
                  size={14}
                  className={styles.searchIcon}
                  aria-hidden="true"
                />
                <input
                  className="nj-field"
                  type="search"
                  placeholder="Search titles, sessions, files…"
                  aria-label="Search artifacts"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
              </label>
              {!mobile && selects}
              <Seg<KindF>
                label="Kind"
                value={kind}
                onChange={(v) => setParam("kind", v)}
                options={[
                  { value: "all", label: "All" },
                  { value: "site", label: "Sites" },
                  { value: "file", label: "Files" },
                ]}
              />
              <Seg<VisF>
                label="Visibility"
                value={vis}
                onChange={(v) => setParam("visibility", v)}
                options={[
                  { value: "all", label: "All" },
                  { value: "private", label: "Private" },
                  { value: "public", label: "Public" },
                ]}
              />
              {mobile && (
                <>
                  <button
                    type="button"
                    className={styles.moreFilters}
                    aria-expanded={moreOpen}
                    onClick={() => setMoreOpen((v) => !v)}
                    data-testid="artifacts-more-filters"
                  >
                    <SlidersHorizontal size={14} aria-hidden="true" />
                    Project · session · mission
                    {hiddenActive ? ` (${hiddenActive})` : ""}
                  </button>
                  {moreOpen && (
                    <div className={styles.moreSelects}>{selects}</div>
                  )}
                </>
              )}
            </div>

            {chips.length > 0 && (
              <div
                className={styles.activeRow}
                data-testid="artifacts-active-filters"
              >
                <span className={styles.lbl}>Filtered by</span>
                {chips.map((c) => (
                  <Chip
                    key={c.key}
                    on
                    onClick={c.clear}
                    title={`Clear ${c.key}`}
                  >
                    <span className={styles.chipX}>
                      {c.label}
                      <X size={12} aria-hidden="true" />
                    </span>
                  </Chip>
                ))}
                {page && settings && (
                  <span className={styles.count}>
                    {page.total} of {settings.artifacts}
                  </span>
                )}
              </div>
            )}

            {error && (
              <p className={styles.error} role="alert">
                {error}
              </p>
            )}
            {page === null && !error ? (
              <Loader
                kind="acquire"
                label="Acquiring artifacts"
                testId="artifacts-loading"
              />
            ) : page && items.length === 0 ? (
              <div className={styles.info} data-testid="artifacts-no-match">
                <Search size={16} aria-hidden="true" />
                <p>No artifact matches these filters.</p>
                <div className={styles.btnRow}>
                  <button
                    type="button"
                    className={styles.ghost}
                    onClick={clearAll}
                  >
                    Clear filters
                  </button>
                </div>
              </div>
            ) : (
              <div className={styles.list} role="list" aria-label="Artifacts">
                <div className={styles.lhead} aria-hidden="true">
                  <span>Kind</span>
                  <span>Title</span>
                  <span>Made by</span>
                  <span>Latest</span>
                  <span>Size</span>
                  <span>Visibility</span>
                  <span />
                </div>
                {items.map((a) => {
                  const sessionTitle = names.session(a.origin.session_key);
                  const projectName = names.project(a.origin.project_id);
                  const missionTitle = names.mission(a.origin.mission_id);
                  return (
                    <div
                      key={a.id}
                      className={styles.row}
                      role="listitem"
                      data-artifact={a.id}
                    >
                      <div className={styles.kindCol}>
                        <KindTag kind={a.kind} entry={a.entry} />
                      </div>
                      <div className={styles.main}>
                        <div className={styles.mtop}>
                          <KindTag kind={a.kind} entry={a.entry} />
                          <span className={styles.meta}>
                            <b>{revLabel(a.revision)}</b>
                            <span className={styles.dot}>
                              {ago(a.updated_at)}
                            </span>
                            <span className={styles.dot}>
                              {formatSize(a.bytes)}
                            </span>
                            {a.files > 1 && (
                              <span className={styles.dot}>
                                {filesLabel(a.files)}
                              </span>
                            )}
                          </span>
                        </div>
                        <Link
                          className={styles.title}
                          to={artifactPath(a.id)}
                          title={a.title}
                        >
                          {a.title}
                        </Link>
                        <p className={styles.why} title={a.why}>
                          {a.why}
                        </p>
                      </div>
                      <div className={styles.prov}>
                        <div className={styles.provTop}>
                          <span className={styles.eng}>{a.origin.engine}</span>
                          <span
                            className={styles.ell}
                            title={sessionTitle ?? a.origin.session_key}
                          >
                            {sessionTitle ?? a.origin.session_key}
                          </span>
                        </div>
                        <div className={styles.provSub}>
                          {(projectName ?? a.origin.cwd) && (
                            <>
                              <Folder size={12} aria-hidden="true" />
                              <span className={styles.ell}>
                                {projectName ?? a.origin.cwd}
                              </span>
                            </>
                          )}
                          {a.origin.mission_id && (
                            <>
                              <Crosshair size={12} aria-hidden="true" />
                              <span className={styles.ell}>
                                {missionTitle ?? a.origin.mission_id}
                              </span>
                            </>
                          )}
                        </div>
                      </div>
                      <div className={styles.num}>
                        {revLabel(a.revision)}
                        <small>{ago(a.updated_at)}</small>
                      </div>
                      <div className={styles.num}>
                        {formatSize(a.bytes)}
                        <small>{filesLabel(a.files)}</small>
                      </div>
                      <div className={styles.visCol}>
                        <VisibilityTag visibility={a.visibility} />
                      </div>
                      <div className={styles.visM}>
                        <VisibilityTag visibility={a.visibility} />
                      </div>
                      <div className={styles.acts}>
                        <a
                          className={styles.rowBtn}
                          href={appUrl(hostedArtifactPath(a.id))}
                          target="_blank"
                          rel="noopener noreferrer"
                          aria-label={`Open ${a.title} in a new tab`}
                        >
                          <ExternalLink size={14} aria-hidden="true" />
                          Open
                        </a>
                        <button
                          type="button"
                          className={styles.rowIcon}
                          aria-label={`Copy the link to ${a.title}`}
                          title="Copy link"
                          onClick={() => void copy(a)}
                        >
                          <Copy size={14} aria-hidden="true" />
                        </button>
                        <RowMenu
                          items={rowMenu(a)}
                          title={a.title}
                          sheetTitle="Artifact actions"
                          triggerLabel={`More for ${a.title}`}
                          triggerIcon={
                            <MoreHorizontal size={16} aria-hidden="true" />
                          }
                          triggerClassName={styles.rowIcon}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            {page?.next_cursor && (
              <div className={styles.more}>
                <button
                  type="button"
                  className={styles.ghost}
                  disabled={loadingMore}
                  onClick={() => void loadMore()}
                >
                  {loadingMore
                    ? "Loading…"
                    : `Show more (${page.total - items.length} left)`}
                </button>
              </div>
            )}
          </>
        )}
        <p className={styles.status} role="status">
          {status}
        </p>
      </div>
      {confirm && (
        <ConfirmDialog
          tag={
            confirm.kind === "revoke" ? "Revoke public link" : "Delete artifact"
          }
          title={
            confirm.kind === "revoke"
              ? `End the public link to “${confirm.a.title}”?`
              : `Delete “${confirm.a.title}”?`
          }
          confirmLabel={confirm.kind === "revoke" ? "Revoke" : "Delete"}
          danger={confirm.kind === "delete"}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void act()}
          returnFocusTo={confirm.returnTo}
          fallbackFocusSelector="[data-testid='artifacts-page'] h1"
        >
          <p className={styles.note}>
            {confirm.kind === "revoke"
              ? "Revoking ends the link on the next request. Sharing again makes a new link; the old one never comes back."
              : "Every revision and its files are removed. Its private and public links stop working. The session's own files are not touched."}
          </p>
        </ConfirmDialog>
      )}
    </div>
  );
}
