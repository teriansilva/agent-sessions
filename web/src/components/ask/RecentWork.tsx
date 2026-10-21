import { X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { useDashboardRetention } from "../../app/dashboardRetentionStore";
import { api } from "../../lib/api";
import { relTime } from "../../lib/format";
import type { RecentWorkEntry, RecentWorkPayload } from "../../types/api";
import { useScopeEpoch } from "../dashboard/useScopeEpoch";
import dlg from "../HudDialog.module.css";
import { useFocusContainment } from "../pulse/useModalDrawer";
import s from "./AskHome.module.css";
import { sessionRoute } from "./needsYouLabels";
import { previewEntries } from "./recentWork";

const hhmm = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayKey = (ts: number) => new Date(ts * 1000).toDateString();
const dayLabel = (ts: number) =>
  new Date(ts * 1000).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });

/** RECENT WORK above Ask (#1086): what you did across sessions, oldest first.
 *
 *  Reading never costs a model call: the server serves its cached summary (or local entries). When
 *  that summary is `stale` and an endpoint exists, the page asks for ONE refresh per visit — and a
 *  failed refresh keeps what was showing. The text is model output built from agent text, so it is
 *  rendered as plain text, never markup. */
export function RecentWork({
  windowDays,
  onWindowDays,
  onRevalidating,
}: {
  windowDays: number;
  onWindowDays: (d: number) => void;
  /** Whether the SNAPSHOT read is re-reading a window that is already painted (#1223) — what the
   *  dashboard's bar tracks. The follow-on AI refresh has its own "updating…" and is not part of
   *  it. */
  onRevalidating?: (revalidating: boolean) => void;
}) {
  // #1223: the last snapshot per window is retained above the router, so coming back paints it
  // at once. The read below still runs every mount — retention decides what is drawn first, never
  // whether to read.
  const store = useDashboardRetention();
  const storeRef = useRef(store);
  useEffect(() => {
    storeRef.current = store;
  }, [store]);
  // Everything held is TAGGED with the window it answers (review 5188): a new window must never
  // show the previous window's result — or its "updating…" — as its own.
  const [got, setGot] = useState<{ window: number; payload: RecentWorkPayload } | null>(null);
  const [failed, setFailed] = useState<number | null>(null);
  const [refreshingFor, setRefreshingFor] = useState<number | null>(null);
  const [readingFor, setReadingFor] = useState<number | null>(null);
  const data =
    got && got.window === windowDays
      ? got.payload
      : (store.read<RecentWorkPayload>(`recentWork:${windowDays}`) ?? null);
  const error = failed === windowDays;
  const refreshing = refreshingFor === windowDays;
  const revalidating = readingFor === windowDays && data !== null;
  useEffect(() => onRevalidating?.(revalidating), [onRevalidating, revalidating]);
  useEffect(() => () => onRevalidating?.(false), [onRevalidating]);
  const [more, setMore] = useState(false);
  const refreshedFor = useRef<number | null>(null);
  const gen = useRef(0);
  // Another scope's summary is not this scope's: drop what is painted and re-read. A new scope is
  // a new question, so its stale snapshot gets its own AI-refresh attempt too.
  const scopeEpoch = useScopeEpoch(store.scopeKey, gen, () => {
    setGot(null);
    refreshedFor.current = null;
  });

  useEffect(() => {
    const mine = ++gen.current;
    (async () => {
      const w = windowDays;
      const forScope = storeRef.current.scopeKey;
      setReadingFor(w);
      try {
        const read = await api.recentWork(w);
        if (mine !== gen.current) return;
        setReadingFor((cur) => (cur === w ? null : cur));
        setFailed((f) => (f === w ? null : f));
        setGot({ window: w, payload: read });
        storeRef.current.write(`recentWork:${w}`, read, forScope);
        // One refresh per window per visit, only when a refresh would write something new.
        if (read.stale && read.configured && refreshedFor.current !== w) {
          refreshedFor.current = w;
          setRefreshingFor(w);
          try {
            const fresh = await api.refreshRecentWork(w);
            if (mine === gen.current) {
              setGot({ window: w, payload: fresh });
              storeRef.current.write(`recentWork:${w}`, fresh, forScope);
            }
          } catch {
            /* keep what is showing for THIS window; the next visit tries again */
          } finally {
            // Always settles its OWN window's flag, even after the operator moved on.
            setRefreshingFor((cur) => (cur === w ? null : cur));
          }
        }
      } catch {
        if (mine !== gen.current) return;
        setReadingFor((cur) => (cur === w ? null : cur));
        setFailed(w);
      }
    })();
    // Leaving (or a newer read) retires this one. Its writes land in the SHELL's retention, which
    // outlives this component: an older read resolving after a newer visit's must not replace it.
    const generation = gen;
    return () => {
      generation.current++;
    };
  }, [windowDays, scopeEpoch]);

  const entries = data?.entries ?? [];
  const preview = previewEntries(entries);

  return (
    <section className={s.sec} aria-labelledby="recent-work-h" data-testid="recent-work">
      <div className={s.head}>
        <span className={s.sq} aria-hidden="true" />
        <h2 id="recent-work-h" className={s.title}>
          Recent work
        </h2>
        <span className={s.meta}>
          {refreshing
            ? "updating…"
            : data?.source === "local"
              ? data.configured
                ? "listed from your sessions"
                : "no AI endpoint — listed from your sessions"
              : data?.generated_at
                ? `what you did · oldest first · updated ${relTime(data.generated_at)}`
                : "what you did · oldest first"}
        </span>
        <div className={s.headEnd}>
          {entries.length > 0 ? (
            <button
              type="button"
              className={s.btn}
              onClick={() => setMore(true)}
              data-testid="recent-work-more"
            >
              Show more
            </button>
          ) : null}
          <WindowPicker value={windowDays} onChange={onWindowDays} />
        </div>
      </div>
      {error && data ? (
        // A failed re-read over a painted window says so, like every dashboard tile (#1223).
        <p className={`${s.note} ${s.err}`} role="alert" data-testid="recent-work-refresh-error">
          Couldn’t refresh — showing the last read.
        </p>
      ) : null}
      {error && !data ? (
        <p className={`${s.note} ${s.err}`} role="alert" data-testid="recent-work-error">
          Couldn’t read your recent work for this window.
        </p>
      ) : !data ? (
        <p className={s.note}>Reading…</p>
      ) : entries.length === 0 ? (
        <p className={s.note}>Nothing in this window yet.</p>
      ) : (
        <div className={s.timeline}>
          {preview.map((e) => (
            <Entry key={`${e.session_key}-${e.ts}`} e={e} />
          ))}
        </div>
      )}
      {/* Stays open across a window change: it reads, or fails, for the window it now names. */}
      {more ? (
        <RecentWorkDialog
          data={data}
          failed={error}
          windowDays={windowDays}
          onWindowDays={onWindowDays}
          onClose={() => setMore(false)}
        />
      ) : null}
    </section>
  );
}

export function WindowPicker({
  value,
  onChange,
}: {
  value: number;
  onChange: (d: number) => void;
}) {
  return (
    <div className={s.seg} role="radiogroup" aria-label="Recent work window">
      {[1, 2, 3].map((d) => (
        <label key={d}>
          <input
            type="radio"
            name="recent-window"
            checked={value === d}
            onChange={() => onChange(d)}
          />
          {d === 1 ? "1 day" : `${d}`}
        </label>
      ))}
    </div>
  );
}

function Entry({ e, expandable }: { e: RecentWorkEntry; expandable?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={s.entry} data-testid="recent-work-entry">
      <span className={s.at}>{hhmm(e.ts)}</span>
      <div className={s.what}>
        <b>{e.title || e.session_key}</b> — {e.text}
        <span className={s.chip}>
          {e.engine}
          {e.project.name ? ` · ${e.project.name}` : ""}
        </span>
        {open ? <div className={s.recap}>{e.session_recap || "No recap stored."}</div> : null}
      </div>
      {expandable ? (
        <div className={s.ctl}>
          <button
            type="button"
            className={`${s.btn} ${s.icon}`}
            aria-expanded={open}
            aria-label={open ? "Hide this session’s recap" : "Show this session’s recap"}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? "▾" : "▸"}
          </button>
          <Link className={s.btn} to={sessionRoute(e.session_key)}>
            Jump in
          </Link>
        </div>
      ) : (
        <span />
      )}
    </div>
  );
}

/** Recent work → Show more: the whole window, by day, filterable, each entry expandable. */
function RecentWorkDialog({
  data,
  failed,
  windowDays,
  onWindowDays,
  onClose,
}: {
  /** null while the selected window is being read, or when its read failed (`failed`). */
  data: RecentWorkPayload | null;
  failed: boolean;
  windowDays: number;
  onWindowDays: (d: number) => void;
  onClose: () => void;
}) {
  const [engine, setEngine] = useState("");
  const [project, setProject] = useState("");
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useFocusContainment({ active: true, panelRef: panel });
  useEffect(() => closeRef.current?.focus(), []);
  const onKey = useCallback(
    (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        onClose();
      }
    },
    [onClose],
  );
  useEffect(() => {
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onKey]);

  const all = useMemo(() => data?.entries ?? [], [data]);
  const engines = useMemo(
    () => [...new Set(all.map((e) => e.engine).filter(Boolean))].sort(),
    [all],
  );
  const projects = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of all) if (e.project.id) m.set(e.project.id, e.project.name);
    return [...m.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [all]);
  const shown = [...all]
    .filter((e) => (!engine || e.engine === engine) && (!project || e.project.id === project))
    .sort((a, b) => a.ts - b.ts);
  const days: [string, RecentWorkEntry[]][] = [];
  for (const e of shown) {
    const k = dayKey(e.ts);
    const last = days[days.length - 1];
    if (last && last[0] === k) last[1].push(e);
    else days.push([k, [e]]);
  }

  return createPortal(
    <div className={dlg.backdrop} onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="recent-work-dialog-h"
        className={dlg.dialog}
        onMouseDown={(ev) => ev.stopPropagation()}
        data-testid="recent-work-dialog"
      >
        <div className={dlg.head}>
          <span id="recent-work-dialog-h" className={dlg.tag}>
            Recent work
          </span>
          <button
            ref={closeRef}
            type="button"
            className={dlg.close}
            onClick={onClose}
            aria-label="Close recent work"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className={s.bar}>
          <WindowPicker value={windowDays} onChange={onWindowDays} />
          <select
            className={s.select}
            aria-label="Agent"
            value={engine}
            onChange={(ev) => setEngine(ev.target.value)}
          >
            <option value="">Agent · all</option>
            {engines.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
          <select
            className={s.select}
            aria-label="Project"
            value={project}
            onChange={(ev) => setProject(ev.target.value)}
          >
            <option value="">Project · all</option>
            {projects.map(([id, name]) => (
              <option key={id} value={id}>
                {name || id}
              </option>
            ))}
          </select>
          <span className={s.sp} />
          <span className={s.meta}>
            {shown.length} {shown.length === 1 ? "entry" : "entries"} · oldest first
          </span>
        </div>
        {!data ? (
          failed ? (
            <p className={`${s.note} ${s.err}`} role="alert" data-testid="recent-work-error">
              Couldn’t read your recent work for this window.
            </p>
          ) : (
            <p className={s.note}>Reading…</p>
          )
        ) : days.length === 0 ? (
          <p className={s.note}>Nothing matches these filters.</p>
        ) : (
          days.map(([k, list]) => (
            <div key={k}>
              <div className={s.day}>{dayLabel(list[0].ts)}</div>
              {list.map((e) => (
                <Entry key={`${e.session_key}-${e.ts}`} e={e} expandable />
              ))}
            </div>
          ))
        )}
      </div>
    </div>,
    document.body,
  );
}
