/** Export… / Import… for Library → Skills and → Playbooks (#1714 P1, boards in the issue's P0).
 *
 *  Export: pick items; whatever a chosen item needs is shown as included and can't be unticked on
 *  its own; the server writes one `.battlelab` file. Import: choose a file → the server verifies
 *  it and answers a preview (renames, refusals, every file's text) → the operator confirms a
 *  subset → one result per item. Nothing changes before the confirm, and nothing imported is
 *  activated, deployed, launched or made a default. */
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  Ban,
  Check,
  ChevronRight,
  Download,
  FileText,
  Lock,
  RefreshCw,
  Upload,
  X,
} from "lucide-react";
import { api, ApiError } from "../../lib/api";
import { playbookPath, SKILLS_PATH } from "../../lib/routes";
import type {
  ShareCatalog,
  ShareKind,
  SharePreview,
  SharePreviewItem,
  ShareResult,
  ShareResultItem,
} from "../../types/share";
import buttons from "../ui/actionButton.module.css";
import hud from "../HudDialog.module.css";
import {
  asCatalog,
  asPreview,
  closure,
  initialSelection,
  keyOf,
  MAX_BUNDLE_BYTES,
  previewStates,
  sizeText,
} from "./shareRules";
import { sharedImportMemory, unshown, type ImportMemory } from "./shareRules";
import styles from "./share.module.css";

const KIND_LABEL: Record<string, string> = { skill: "Skill", playbook: "Playbook" };
const GROUP_LABEL: Record<string, string> = { skill: "Skills", playbook: "Playbooks" };

function errorText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : "Something went wrong.";
}

/** The shared shell: a native modal <dialog> on THE dialog chrome, a scrolling body and a
 *  footer that stays in view. */
function Shell({
  title,
  testId,
  busy,
  onClose,
  children,
  foot,
}: {
  title: string;
  testId: string;
  busy: boolean;
  onClose: () => void;
  children: ReactNode;
  foot: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const node = dialog.current!;
    const opener = document.activeElement as HTMLElement | null;
    if (typeof node.showModal === "function") node.showModal();
    else node.setAttribute("open", "");
    return () => {
      if (typeof node.close === "function") node.close();
      if (opener?.isConnected) opener.focus();
    };
  }, []);
  return createPortal(
    <dialog
      ref={dialog}
      className={`${hud.dialog} ${styles.dialog}`}
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      data-testid={testId}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
    >
      <div className={hud.head}>
        <h2 id={titleId} className={hud.tag}>
          {title}
        </h2>
        <button className={hud.close} aria-label="Close" disabled={busy} onClick={onClose}>
          <X size={16} aria-hidden />
        </button>
      </div>
      <div className={styles.body}>{children}</div>
      <div className={styles.foot}>{foot}</div>
    </dialog>,
    document.body,
  );
}

function Banner({
  tone = "warn",
  icon,
  title,
  children,
}: {
  tone?: "warn" | "down" | "info";
  icon: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div
      className={`${styles.banner} ${tone === "down" ? styles.bannerDown : tone === "info" ? styles.bannerInfo : ""}`}
      role={tone === "down" ? "alert" : undefined}
    >
      {icon}
      <div>
        <strong>{title}</strong>
        {children && <p>{children}</p>}
      </div>
    </div>
  );
}

// ---- export -------------------------------------------------------------------------------------

function ExportDialog({ kind, onClose }: { kind: ShareKind; onClose: () => void }) {
  const [catalog, setCatalog] = useState<ShareCatalog | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState("");
  const live = useRef(true);
  useEffect(() => () => void (live.current = false), []);
  const fetchCatalog = useCallback(
    () =>
      Promise.resolve()
        .then(() => api.shareCatalog())
        .then((value) => {
          if (!live.current) return;
          const parsed = asCatalog(value);
          if (!parsed) setError("The list of exportable items could not be read.");
          setCatalog(parsed);
        })
        .catch((e: unknown) => live.current && setError(errorText(e)))
        .finally(() => live.current && setLoading(false)),
    [],
  );
  useEffect(() => {
    void fetchCatalog();
  }, [fetchCatalog]);
  const load = () => {
    setLoading(true);
    setError("");
    void fetchCatalog();
  };
  const items = useMemo(() => catalog?.items ?? [], [catalog]);
  const included = useMemo(() => closure(items, chosen), [items, chosen]);
  const needle = filter.trim().toLowerCase();
  const order: string[] = kind === "playbook" ? ["playbook", "skill"] : ["skill", "playbook"];
  const total = chosen.size + included.size;
  const toggle = (key: string) =>
    setChosen((old) => {
      const next = new Set(old);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const download = async () => {
    setBusy(true);
    setError("");
    try {
      const keys = [...chosen];
      const blob = await api.shareExport(
        keys.map((k) => ({ kind: k.split(":")[0], id: k.slice(k.indexOf(":") + 1) })),
      );
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, "").replace("T", "-");
      const name = `battlelab-${stamp}.battlelab`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
      if (live.current) setDone(`Downloaded ${name} with ${total} item${total === 1 ? "" : "s"}.`);
    } catch (e) {
      if (live.current) setError(errorText(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };
  return (
    <Shell
      title="Export to a file"
      testId="share-export"
      busy={busy}
      onClose={onClose}
      foot={
        <>
          <p className={styles.sum} data-testid="share-export-summary">
            <b>{total} item{total === 1 ? "" : "s"}</b> · {chosen.size} chosen · {included.size} included
          </p>
          <button className={buttons.ghost} disabled={busy} onClick={onClose}>
            {done ? "Close" : "Cancel"}
          </button>
          <button className={buttons.primary} disabled={busy || !chosen.size} onClick={() => void download()}>
            <Download size={16} aria-hidden />
            Download bundle
          </button>
        </>
      }
    >
      <p className={styles.prose}>
        Choose what to share. Anything a chosen item needs comes along and is shown as <b>included</b>.
        The file holds the chosen items exactly as written; no settings, deployment files or secret
        variables are added to it.
      </p>
      {error && (
        <Banner tone="down" icon={<Ban size={16} aria-hidden />} title="Export failed.">
          {error}{" "}
          {!catalog && (
            <button className={buttons.ghost} onClick={load}>
              Try again
            </button>
          )}
        </Banner>
      )}
      {done && (
        <p role="status" className={`${styles.prose} ${styles.ok}`}>
          {done}
        </p>
      )}
      {loading && !catalog && <p role="status" className={styles.prose}>Loading your library…</p>}
      {catalog && !items.length && (
        <div className={styles.drop}>
          <FileText size={28} aria-hidden />
          <b>Nothing to export yet</b>
          <span>Create a skill or a playbook, or add one from the gallery; it will be listed here.</span>
        </div>
      )}
      {!!items.length && (
        <input
          className={styles.filter}
          type="search"
          aria-label="Filter by name or ID"
          placeholder="Filter by name or ID"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      )}
      {order.map((k) => {
        const rows = items.filter(
          (i) =>
            i.kind === k &&
            (!needle || `${i.title} ${i.id}`.toLowerCase().includes(needle) || included.has(keyOf(i)) || chosen.has(keyOf(i))),
        );
        if (!rows.length) return null;
        const picked = rows.filter((i) => chosen.has(keyOf(i))).length;
        const pulled = rows.filter((i) => included.has(keyOf(i))).length;
        return (
          <section key={k} aria-label={GROUP_LABEL[k]}>
            <h3 className={styles.group}>
              {GROUP_LABEL[k]}{" "}
              <span>
                // {picked} chosen{pulled ? ` · ${pulled} included` : ""}
              </span>
            </h3>
            <ul className={styles.list}>
              {rows.map((i) => {
                const key = keyOf(i);
                const by = included.get(key);
                const isChosen = chosen.has(key);
                const locked = !!by && !isChosen;
                const inputId = `share-x-${key}`;
                return (
                  <li key={key} className={`${styles.row} ${locked ? styles.included : ""}`} data-testid={`share-export-row-${key}`}>
                    <label className={styles.check} htmlFor={inputId}>
                      <input
                        id={inputId}
                        type="checkbox"
                        checked={isChosen || locked}
                        disabled={busy || locked || !i.exportable}
                        aria-label={`${KIND_LABEL[i.kind] ?? i.kind} ${i.title}${locked ? " (included)" : ""}`}
                        onChange={() => toggle(key)}
                      />
                    </label>
                    <div className={styles.name}>
                      <span className={styles.title}>
                        <span className={styles.kind}>{KIND_LABEL[i.kind] ?? i.kind}</span>
                        {i.title}
                      </span>
                      <span className={styles.line}>
                        {locked
                          ? `Included — needed by ${by!.map((b) => b.slice(b.indexOf(":") + 1)).join(", ")}`
                          : `${i.id}${i.source !== "local" ? ` · ${i.source}` : ""}${i.depends_on.length ? ` · needs ${i.depends_on.length} skill${i.depends_on.length === 1 ? "" : "s"}` : ""}`}
                      </span>
                      {!i.exportable && <span className={`${styles.why} ${styles.down}`}>{i.reason ?? "Can't be exported."}</span>}
                    </div>
                    <div className={styles.state}>
                      {locked ? (
                        <span className={`${styles.stamp} ${styles.lock}`}>
                          <Lock size={12} aria-hidden />
                          Included
                        </span>
                      ) : isChosen ? <span className={styles.stamp}>Chosen</span> : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
      {!!items.length && (
        <Banner tone="warn" icon={<AlertTriangle size={16} aria-hidden />} title="Check the text for secrets before you share it.">
          Skills and playbooks travel exactly as written. A token, password or key pasted into one
          of their files goes into the bundle with it. Secret variables themselves are never
          exported.
        </Banner>
      )}
    </Shell>
  );
}

// ---- import -------------------------------------------------------------------------------------

type Phase =
  | { at: "pick"; error?: string; busy?: boolean }
  | { at: "checking" }
  | { at: "preview"; preview: SharePreview; stale?: string; frozen?: "unclear" | "selection" }
  | { at: "confirming"; preview: SharePreview; frozen?: "unclear" | "selection" }
  | { at: "result"; preview: SharePreview; result: ShareResult };

const STALE: Record<string, string> = {
  expired: "This preview expired. Previews last 15 minutes; preview the file again to continue.",
  changed: "Your library changed since this preview, so its plan is out of date. Nothing was imported. Preview the file again before confirming.",
  missing: "This preview is gone. Preview the file again.",
  evicted:
    "This preview's file was cleared to make room for other imports. Nothing was imported. Preview the file again to continue.",
};

function linkTo(item: { kind: string; dest: string | null }): string | null {
  if (!item.dest) return null;
  if (item.kind === "skill") return `${SKILLS_PATH}/${encodeURIComponent(item.dest)}`;
  if (item.kind === "playbook") return playbookPath(item.dest);
  return null;
}

function Files({ item }: { item: SharePreviewItem }) {
  const rewritten = item.kind === "playbook" && (item.dest !== item.id || item.uses.length > 0);
  return (
    <details className={styles.files}>
      <summary>
        Review {item.files.length} file{item.files.length === 1 ? "" : "s"}
      </summary>
      {rewritten && (
        <span className={styles.why}>
          These are the files as sent. On import,{" "}
          {[
            item.dest !== item.id ? `playbook.toml's ID becomes ${item.dest}` : "",
            item.uses.length
              ? `its flows' role skills become ${item.uses.map((u) => u.to).join(", ")}`
              : "",
          ]
            .filter(Boolean)
            .join(" and ")}
          ; each rewritten TOML file is saved without its comments or original layout.
        </span>
      )}
      {item.files.map((f) => (
        <div key={f.path}>
          <span className={styles.fileName}>
            {f.path} · {sizeText(f.size)}
          </span>
          {typeof f.text === "string" ? (
            <pre>{f.text}</pre>
          ) : (
            <span className={styles.why}>
              {f.binary
                ? "Binary file: it can't be shown here."
                : "Too large to show here, so it can't be reviewed before you import it."}
            </span>
          )}
        </div>
      ))}
    </details>
  );
}

function PreviewRow({
  item,
  state,
  checked,
  disabled,
  onToggle,
  already,
  referenced = false,
}: {
  already?: string;
  /** A playbook in this file uses this skill (its reference follows the rename). */
  referenced?: boolean;
  item: SharePreviewItem;
  state: ReturnType<typeof previewStates> extends Map<string, infer S> ? S : never;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const inputId = `share-i-${item.key}`;
  const serverRefused = item.status === "refused";
  const stamp =
    state.state === "refused" ? (
      <span className={`${styles.stamp} ${styles.down}`}>Refused</span>
    ) : state.state === "deselected" ? (
      <span className={styles.stamp}>Deselected</span>
    ) : item.status === "renamed" ? (
      <span className={`${styles.stamp} ${styles.warn}`}>Renamed</span>
    ) : (
      <span className={`${styles.stamp} ${styles.ok}`}>New copy</span>
    );
  return (
    <li className={styles.row} data-testid={`share-preview-row-${item.key}`} data-state={state.state}>
      <label className={styles.check} htmlFor={inputId}>
        <input
          id={inputId}
          type="checkbox"
          checked={checked && !serverRefused}
          disabled={disabled || serverRefused}
          aria-label={`Import ${KIND_LABEL[item.kind] ?? item.kind} ${item.title}`}
          onChange={onToggle}
        />
      </label>
      <div className={styles.name}>
        <span className={styles.title}>
          <span className={styles.kind}>{KIND_LABEL[item.kind] ?? item.kind}</span>
          {item.title}
        </span>
        <span className={styles.line}>
          <b>{item.id}</b>
          {item.dest && item.dest !== item.id ? (
            <>
              {" → "}
              <b>{item.dest}</b>
            </>
          ) : item.dest ? (
            " → new copy"
          ) : null}
          {item.uses.length ? ` · uses ${item.uses.map((u) => u.to).join(", ")}` : ""}
        </span>
        {state.state === "refused" && <span className={`${styles.why} ${styles.down}`}>{state.reason}</span>}
        {state.state !== "refused" && item.status === "renamed" && (
          <span className={`${styles.why} ${styles.warn}`}>
            A {item.kind} named {item.id} already exists here. Yours stays untouched; this copy is {item.dest}
            {item.uses.length
              ? ", and its references are rewritten to match"
              : referenced
                ? `, and playbooks in this file that use it are pointed at ${item.dest}`
                : ""}
            .
          </span>
        )}
        {already && !serverRefused && (
          <span className={`${styles.why} ${styles.warn}`} data-testid="already-imported">
            Already imported from this file as {already}. Ticking it imports a second copy.
          </span>
        )}
        {!serverRefused && <Files item={item} />}
      </div>
      <div className={styles.state}>{stamp}</div>
    </li>
  );
}

const OUTCOME: Record<string, { word: string; tone: string; icon: ReactNode }> = {
  created: { word: "Created", tone: styles.ok, icon: <Check size={16} aria-hidden /> },
  refused: { word: "Refused", tone: styles.down, icon: <Ban size={16} aria-hidden /> },
  failed: { word: "Failed", tone: styles.down, icon: <X size={16} aria-hidden /> },
  skipped: { word: "Skipped", tone: styles.muted, icon: <ArrowRight size={16} aria-hidden /> },
  not_selected: { word: "Not selected", tone: styles.muted, icon: <span aria-hidden /> },
  stale: { word: "Preview again", tone: styles.warn, icon: <RefreshCw size={16} aria-hidden /> },
};

function ResultRow({ item, onOpen }: { item: ShareResultItem; onOpen: () => void }) {
  const o = OUTCOME[item.status] ?? OUTCOME.failed;
  const href = item.status === "created" ? linkTo(item) : null;
  return (
    <li className={styles.row} data-testid={`share-result-row-${item.key}`} data-status={item.status}>
      <span className={`${styles.mark} ${o.tone}`}>{o.icon}</span>
      <div className={styles.name}>
        <span className={styles.title}>
          <span className={styles.kind}>{KIND_LABEL[item.kind] ?? item.kind}</span>
          {item.title}
        </span>
        <span className={styles.line}>
          {item.dest ?? item.id}
          {item.dest && item.dest !== item.id ? ` (renamed from ${item.id})` : ""}
        </span>
        {item.reason && <span className={`${styles.why} ${o.tone}`}>{item.reason}</span>}
      </div>
      <div className={styles.state}>
        <span className={`${styles.stamp} ${o.tone}`}>{o.word}</span>
        {href && (
          <Link className={styles.open} to={href} onClick={onOpen}>
            Open
            <ChevronRight size={14} aria-hidden />
          </Link>
        )}
      </div>
    </li>
  );
}

/** How many copies the library says came from this bundle (GET /api/share/imports: each entry's
 *  `bundle` is the digest). Anything but a well-formed list (a failed read, the server's 503 for
 *  an unreadable record, a stub `{}`) is `null`: unknown, so the preview holds everything
 *  unticked rather than failing open. */
async function copiesFrom(digest: string): Promise<number | null> {
  try {
    const value = await Promise.resolve().then(() => api.shareImports());
    const imports = value && typeof value === "object" ? (value as { imports?: unknown }).imports : null;
    if (!imports || typeof imports !== "object" || Array.isArray(imports)) return null;
    return Object.values(imports as Record<string, unknown>).filter(
      (v) => !!v && typeof v === "object" && (v as { bundle?: unknown }).bundle === digest,
    ).length;
  } catch {
    return null;
  }
}

function asResult(value: unknown): ShareResult | null {
  if (!value || typeof value !== "object") return null;
  const r = value as { items?: unknown; token?: unknown };
  if (typeof r.token !== "string" || !Array.isArray(r.items)) return null;
  return value as ShareResult;
}

function ImportDialog({
  onClose,
  onImported,
  memory,
}: {
  onClose: () => void;
  onImported: () => void;
  memory: ImportMemory;
}) {
  const [phase, setPhase] = useState<Phase>({ at: "pick" });
  const [file, setFile] = useState<File | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [over, setOver] = useState(false);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const live = useRef(true);
  const created = useRef(false);
  // What earlier confirms of THIS bundle created (by digest, from the page-level memory): a fresh
  // preview of the same file starts with them unticked and labelled, never ticked again.
  const [alreadyView, setAlreadyView] = useState<ReadonlyMap<string, { dest: string; token: string }>>(
    new Map(),
  );
  // Copies the library says came from this bundle, and whether nothing starts ticked because what
  // was applied isn't known item by item (a reload, another tab, an unclear confirm).
  const [fromLibrary, setFromLibrary] = useState<number | null>(0);
  // Files this preview couldn't show: importing them needs an explicit acknowledgement.
  const [ack, setAck] = useState(false);
  const [held, setHeld] = useState(false);
  const done = useRef<HTMLButtonElement>(null);
  const settle = useRef<HTMLButtonElement>(null);
  const frozenNow = phase.at === "preview" || phase.at === "confirming" ? phase.frozen : undefined;
  useEffect(() => () => void (live.current = false), []);
  useEffect(() => {
    if (phase.at === "result") done.current?.focus();
  }, [phase.at]);
  useEffect(() => {
    if (frozenNow && phase.at === "preview") settle.current?.focus();
  }, [frozenNow, phase.at]);
  const busy = phase.at === "checking" || phase.at === "confirming";
  const close = () => {
    if (created.current) onImported();
    onClose();
  };
  const check = async (f: File) => {
    setFile(f);
    setError("");
    if (f.size > MAX_BUNDLE_BYTES) {
      setPhase({ at: "pick", error: `${f.name} is larger than ${sizeText(MAX_BUNDLE_BYTES)}; a bundle is at most that size.` });
      return;
    }
    setPhase({ at: "checking" });
    try {
      const preview = asPreview(await api.sharePreview(f));
      if (!live.current) return;
      if (!preview) throw new Error("The preview could not be read.");
      const digest = preview.bundle.digest;
      const known = new Map(memory.created.get(digest) ?? []);
      const library = await copiesFrom(digest);
      if (!live.current) return;
      // Nothing starts ticked when copies of this bundle may exist that this page can't name.
      const hold =
        memory.unclear.has(digest) || library === null || (library > known.size && library > 0);
      const labelled = new Map([...known].filter(([, v]) => v.token !== preview.token));
      setAlreadyView(labelled);
      setFromLibrary(library);
      setHeld(hold);
      setAck(false);
      setSelected(
        hold
          ? new Set()
          : initialSelection(preview, new Map([...labelled].map(([k, v]) => [k, v.dest]))),
      );
      setPhase({ at: "preview", preview });
    } catch (e) {
      // A 503 is the server being full or busy (staging_full), not this file being refused.
      if (live.current)
        setPhase({ at: "pick", error: errorText(e), busy: e instanceof ApiError && e.status === 503 });
    }
  };
  const confirm = async (preview: SharePreview, frozen?: "unclear" | "selection") => {
    setPhase({ at: "confirming", preview, frozen });
    setError("");
    try {
      const result = asResult(await api.shareConfirm(preview.token, [...selected]));
      if (!live.current) return;
      if (!result) throw new Error("The import answer could not be read.");
      const digest = preview.bundle.digest;
      const record = memory.created.get(digest) ?? new Map();
      for (const item of result.items)
        if (item.status === "created" && item.dest)
          record.set(item.key, { dest: item.dest, token: preview.token });
      memory.created.set(digest, record);
      if (result.items.some((i) => i.status === "created")) created.current = true;
      // A failed item may still have been published (the server couldn't tell): a fresh preview of
      // this bundle starts with nothing ticked.
      if (result.items.some((i) => i.status === "failed")) memory.unclear.add(digest);
      setPhase({ at: "result", preview, result });
    } catch (e) {
      if (!live.current) return;
      const status = e instanceof ApiError ? e.status : 0;
      const reason = e instanceof ApiError ? (e.record as { reason?: string } | undefined)?.reason : undefined;
      if (reason && STALE[reason]) setPhase({ at: "preview", preview, stale: reason });
      else if (frozen === "unclear" && reason !== "selection") {
        // Still unclear: a refusal of the retry doesn't prove the first confirm didn't land.
        setError(errorText(e));
        setPhase({ at: "preview", preview, frozen: "unclear" });
      } else if (reason === "selection") {
        memory.unclear.add(preview.bundle.digest);
        // The server already holds a confirm of this preview: items may exist. Nothing here may
        // be re-selected or re-previewed blindly; the library is re-read on close.
        created.current = true;
        setPhase({ at: "preview", preview, frozen: "selection" });
      } else if (status >= 400 && status < 500) {
        // A refusal before anything was applied (422, 404): the preview stays editable.
        setError(errorText(e));
        setPhase({ at: "preview", preview });
      } else {
        // A lost response or a server/storage error: the confirm may have landed. Only the same
        // selection may be sent again (a retry never makes a second copy).
        memory.unclear.add(preview.bundle.digest);
        created.current = true;
        setError(errorText(e));
        setPhase({ at: "preview", preview, frozen: "unclear" });
      }
    }
  };
  const again = () => file && void check(file);

  let body: ReactNode;
  let foot: ReactNode;
  if (phase.at === "pick" || phase.at === "checking") {
    body = (
      <>
        {phase.at === "pick" && phase.error && (
          <Banner
            tone={phase.busy ? "warn" : "down"}
            icon={phase.busy ? <RefreshCw size={16} aria-hidden /> : <Ban size={16} aria-hidden />}
            title={phase.busy ? "Imports can't be previewed right now." : "This file can't be imported."}
          >
            {phase.error} Nothing was read into your library.
          </Banner>
        )}
        {phase.at === "checking" ? (
          <div className={styles.drop} role="status">
            <RefreshCw size={28} aria-hidden />
            <b>Checking {file?.name}…</b>
            <span>Every file's digest is verified before anything is shown.</span>
          </div>
        ) : (
          <div
            className={`${styles.drop} ${over ? styles.dropActive : ""}`}
            data-testid="share-drop"
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setOver(false);
              const f = e.dataTransfer.files[0];
              if (f) void check(f);
            }}
          >
            <Upload size={28} aria-hidden />
            <b>Choose a .battlelab file</b>
            <span>or drop it here · at most {sizeText(MAX_BUNDLE_BYTES)} · nothing changes until you confirm</span>
            <input
              ref={input}
              className={styles.hiddenInput}
              type="file"
              accept=".battlelab,application/x-tar"
              aria-label="Bundle file"
              data-testid="share-file"
              tabIndex={-1}
              onChange={(e) => {
                const f = e.target.files?.[0];
                e.target.value = "";
                if (f) void check(f);
              }}
            />
            <button className={buttons.ghost} onClick={() => input.current?.click()}>
              {phase.error ? "Choose another file…" : "Choose file…"}
            </button>
          </div>
        )}
      </>
    );
    foot = (
      <>
        <span className={styles.sum} />
        <button className={buttons.ghost} disabled={busy} onClick={close}>
          Cancel
        </button>
      </>
    );
  } else if (phase.at === "preview" || phase.at === "confirming") {
    const { preview } = phase;
    const labelled = new Map(
      [...alreadyView].filter(([, v]) => v.token !== preview.token).map(([k, v]) => [k, v.dest]),
    );
    const states = previewStates(preview.items, selected, labelled);
    const frozen = phase.frozen;
    const will = preview.items.filter((i) => states.get(i.key)?.state === "import");
    const wont = preview.items.filter((i) => states.get(i.key)?.state !== "import");
    const renamed = will.filter((i) => i.status === "renamed").length;
    const refused = wont.filter((i) => states.get(i.key)?.state === "refused").length;
    // Files the operator is about to import without having been able to read them here.
    const blind = will.reduce((n, i) => n + unshown(i), 0);
    const stale = phase.at === "preview" ? phase.stale : undefined;
    const toggle = (key: string) => {
      setAck(false);
      setSelected((old) => {
        const next = new Set(old);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    };
    const rows = (list: SharePreviewItem[]) => (
      <ul className={styles.list}>
        {list.map((item) => (
          <PreviewRow
            key={item.key}
            item={item}
            state={states.get(item.key)!}
            checked={selected.has(item.key)}
            disabled={busy || !!stale || !!frozen}
            onToggle={() => toggle(item.key)}
            already={labelled.get(item.key)}
            referenced={
              item.kind === "skill" &&
              preview.items.some((o) => o.kind === "playbook" && o.uses.some((u) => u.from === item.id))
            }
          />
        ))}
      </ul>
    );
    body = (
      <>
        {stale && (
          <Banner icon={<RefreshCw size={16} aria-hidden />} title={stale === "expired" ? "This preview expired." : "Preview again."}>
            {STALE[stale]}
          </Banner>
        )}
        {fromLibrary === null && !frozen && (
          <Banner icon={<AlertTriangle size={16} aria-hidden />} title="Couldn't check whether this file was imported before.">
            Nothing is ticked: check your library, then tick only what you want to import. Each item
            you tick becomes a copy.
          </Banner>
        )}
        {fromLibrary !== null && fromLibrary > 0 && !frozen && (
          <Banner icon={<AlertTriangle size={16} aria-hidden />} title={`${fromLibrary} cop${fromLibrary === 1 ? "y was" : "ies were"} imported from this file before.`}>
            {held
              ? "Nothing is ticked: tick only what you want to import again. Each item you tick becomes another copy."
              : "The items this dialog already imported are unticked. Ticking an item imports another copy."}
          </Banner>
        )}
        {fromLibrary === 0 && held && !frozen && (
          <Banner icon={<AlertTriangle size={16} aria-hidden />} title="An earlier import of this file may have been applied.">
            Nothing is ticked: check your library, then tick only what you want to import. Each item you
            tick becomes a copy.
          </Banner>
        )}
        {frozen === "unclear" && (
          <Banner tone="down" icon={<AlertTriangle size={16} aria-hidden />} title="The import may have been applied.">
            {error ? `${error} ` : ""}Its answer didn't arrive, so some items may already be in your
            library. Try again with the same selection: that never makes a second copy. The
            selection can't be changed now.
          </Banner>
        )}
        {frozen === "selection" && (
          <Banner tone="down" icon={<AlertTriangle size={16} aria-hidden />} title="This preview was already confirmed.">
            Some of its items may already be in your library. Close this dialog and check your library
            before importing this file again; importing it again creates second copies.
          </Banner>
        )}
        {error && !frozen && (
          <Banner tone="down" icon={<Ban size={16} aria-hidden />} title="Import failed.">
            {error}
          </Banner>
        )}
        <Banner icon={<AlertTriangle size={16} aria-hidden />} title="Not signed — imported from a file.">
          Skills and playbooks are instructions your agents will read. Review them before you import.
          Nothing is activated, deployed, launched or made a default.
        </Banner>
        <dl className={styles.meta} data-testid="share-bundle-meta">
          <dt>File</dt>
          <dd>
            {file?.name ?? "bundle"} · {sizeText(preview.bundle.size)}
          </dd>
          <dt>Made</dt>
          <dd>
            {preview.bundle.created_at} · BattleLab {preview.bundle.app_version}
            {preview.bundle.source ? ` · “${preview.bundle.source}”` : ""}
          </dd>
          <dt>Digest</dt>
          <dd>
            sha256 {preview.bundle.digest.slice(0, 4)}…{preview.bundle.digest.slice(-4)} · verified,{" "}
            {preview.bundle.items} items, {preview.bundle.files} files
          </dd>
        </dl>
        {!will.length && !wont.some((i) => states.get(i.key)?.state === "deselected") && (
          <Banner tone="down" icon={<Ban size={16} aria-hidden />} title="Nothing in this file can be imported.">
            Every item was refused. The reasons are listed on each item.
          </Banner>
        )}
        {!!will.length && (
          <section aria-label="Will import">
            <h3 className={styles.group}>
              Will import <span>// {will.length} of {preview.items.length} selected</span>
            </h3>
            {rows(will)}
          </section>
        )}
        {!!wont.length && (
          <section aria-label="Won't import">
            <h3 className={styles.group}>
              Won't import <span>// {wont.length}</span>
            </h3>
            {rows(wont)}
          </section>
        )}
        {blind > 0 && !frozen && (
          <label className={styles.ack} data-testid="share-blind-ack">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            <span>
              {blind} file{blind === 1 ? "" : "s"} you chose to import couldn't be shown here (too
              large or not text). Agents may read {blind === 1 ? "it" : "them"} as instructions.
              Import {blind === 1 ? "it" : "them"} without review anyway.
            </span>
          </label>
        )}
      </>
    );
    foot = (
      <>
        <p className={styles.sum} data-testid="share-preview-summary">
          <b>{will.length} to import</b>
          {renamed ? ` · ${renamed} renamed` : ""}
          {refused ? ` · ${refused} refused` : ""}
          {blind > 0 && !ack ? ` · ${blind} unreviewed file${blind === 1 ? "" : "s"} need${blind === 1 ? "s" : ""} your OK` : ""}{" "}
          · nothing changes until you confirm
        </p>
        {frozen ? (
          <button
            ref={frozen === "selection" ? settle : undefined}
            className={buttons.ghost}
            disabled={busy}
            onClick={close}
          >
            Close
          </button>
        ) : (
          <button className={buttons.ghost} disabled={busy} onClick={() => setPhase({ at: "pick" })}>
            Back
          </button>
        )}
        {frozen === "selection" ? null : frozen === "unclear" ? (
          <button ref={settle} className={buttons.primary} disabled={busy} onClick={() => void confirm(preview, "unclear")}>
            <RefreshCw size={16} aria-hidden />
            {phase.at === "confirming" ? "Trying again…" : "Try again (same selection)"}
          </button>
        ) : stale ? (
          <button className={buttons.primary} disabled={busy || !file} onClick={again}>
            <RefreshCw size={16} aria-hidden />
            Preview again
          </button>
        ) : (
          <button className={buttons.primary} disabled={busy || !will.length || (blind > 0 && !ack)} onClick={() => void confirm(preview)}>
            {phase.at === "confirming" ? "Importing…" : `Import ${will.length} item${will.length === 1 ? "" : "s"}`}
          </button>
        )}
      </>
    );
  } else {
    const { result } = phase;
    const shown = result.items.filter((i) => i.status !== "not_selected");
    const count = (s: string) => shown.filter((i) => i.status === s).length;
    // What the operator chose and BattleLab attempted: refusals were never going to be created.
    const chosen = shown.filter((i) => i.status !== "refused").length;
    body = (
      <>
        {result.repreview && (
          <Banner icon={<RefreshCw size={16} aria-hidden />} title="Some items need a new preview.">
            {STALE[result.repreview]} Items already imported stay imported.
          </Banner>
        )}
        <p className={styles.prose} role="status">
          {count("created")} of the {chosen} item{chosen === 1 ? "" : "s"} you chose{" "}
          {chosen === 1 ? "was" : "were"} imported. Each is a new local copy marked{" "}
          <b>Imported from a file</b>; nothing that was here changed.
        </p>
        <ul className={styles.list}>
          {shown.map((item) => (
            <ResultRow key={item.key} item={item} onOpen={close} />
          ))}
        </ul>
      </>
    );
    foot = (
      <>
        <p className={styles.sum}>
          <b>{count("created")} created</b>
          {count("failed") ? ` · ${count("failed")} failed` : ""}
          {count("skipped") ? ` · ${count("skipped")} skipped` : ""}
          {count("refused") ? ` · ${count("refused")} refused` : ""}
        </p>
        {(result.repreview || count("failed") > 0) && (
          <button className={buttons.ghost} onClick={result.repreview ? again : () => void confirm(phase.preview)}>
            <RefreshCw size={16} aria-hidden />
            {result.repreview ? "Preview again" : "Retry failed items"}
          </button>
        )}
        <button ref={done} className={buttons.primary} onClick={close}>
          Done
        </button>
      </>
    );
  }
  return (
    <Shell title={phase.at === "result" ? "Import result" : "Import from a file"} testId="share-import" busy={busy} onClose={close} foot={foot}>
      {body}
    </Shell>
  );
}

/** The Import… / Export… pair a Library section's header carries. */
export function ShareActions({
  kind,
  onImported,
  children,
}: {
  kind: ShareKind;
  onImported: () => void;
  /** The section's own primary action, kept beside the pair. */
  children?: ReactNode;
}) {
  const [open, setOpen] = useState<"export" | "import" | null>(null);
  // Outlives each dialog: reopening Import… remembers what earlier imports of a file created.
  const memory = sharedImportMemory;
  return (
    <div className={styles.actions}>
      <button className={buttons.ghost} onClick={() => setOpen("import")}>
        <Upload size={16} aria-hidden />
        Import…
      </button>
      <button className={buttons.ghost} onClick={() => setOpen("export")}>
        <Download size={16} aria-hidden />
        Export…
      </button>
      {children}
      {open === "export" && <ExportDialog kind={kind} onClose={() => setOpen(null)} />}
      {open === "import" && (
        <ImportDialog onClose={() => setOpen(null)} onImported={onImported} memory={memory} />
      )}
    </div>
  );
}

/** The *imported from a file* mark on a Library card. */
export function ImportedMark() {
  return (
    <span className={styles.imported} data-testid="imported-from-file">
      <FileText size={12} aria-hidden />
      Imported from a file
    </span>
  );
}
