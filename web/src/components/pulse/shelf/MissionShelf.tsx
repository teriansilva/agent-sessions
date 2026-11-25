/** The mission's SHELF view (#1740 P1a, board 2): everything the mission produced, as a gallery to
 *  review — the head with the unreviewed count, kind chips and source / grouping controls, and the
 *  tiles grouped by the session that produced them. A tile opens the lightbox; verdicts are set
 *  only there. */
import { ChevronRight, GitBranch } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../../../lib/api";
import { sessionPathFromKey } from "../../../lib/format";
import type { ShelfItem, ShelfKind, ShelfVerdict } from "../../../types/shelf";
import { ShelfLightbox, type VerdictResult } from "./ShelfLightbox";
import { ShelfTile } from "./ShelfTile";
import { KIND_ICON } from "./shelfIcons";
import {
  KIND_LABEL,
  KIND_ORDER,
  NO_FILTERS,
  applyFilters,
  engineOf,
  groupBySession,
  type ShelfFilters,
} from "./shelfLogic";
import type { MissionShelf as ShelfData } from "./useMissionShelf";
import styles from "./shelf.module.css";
import { MissionDebrief } from "./MissionDebrief";
import { useMissionDebrief } from "./useMissionDebrief";

/** Mission states the debrief is shown in (#1740 P2): in review, or closed. */
const DEBRIEF_STATES = new Set(["review", "done", "failed", "abandoned"]);

export function MissionShelf({
  missionId,
  data,
  sessionTitles,
  missionState,
  configured = false,
  objectivesKey = "",
}: {
  missionId: string;
  data: ShelfData;
  /** Session key → title, from the cards the console already holds. */
  sessionTitles: Record<string, string>;
  /** The mission's state: in review or closed, the debrief opens the shelf (#1740 P2). */
  missionState?: string;
  /** An AI endpoint is configured (the debrief writes a summary; without one it is the records). */
  configured?: boolean;
  /** Changes when an objective's state or verdict does: the debrief's records moved. */
  objectivesKey?: string;
}) {
  const { shelf, error, reload, applyVerdict } = data;
  const [filters, setFilters] = useState<ShelfFilters>(NO_FILTERS);
  const [open, setOpen] = useState<string | null>(null);
  /** The order the lightbox walks, frozen when it opens: a verdict under "Unreviewed only" takes
   *  the item out of the grid, and it must not also take it out from under the operator. */
  const [walk, setWalk] = useState<string[] | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const tiles = useRef(new Map<string, HTMLButtonElement>());

  const items = useMemo(() => shelf?.items ?? [], [shelf]);
  const revision = shelf?.revision ?? 0;
  const debriefOn = !!missionState && DEBRIEF_STATES.has(missionState);
  /** Each file's integrity is re-checked on every shelf read and is part of the debrief's records,
   *  but a file changing on disk never moves the shelf's revision: it is in the key itself. */
  const fileStates = useMemo(
    () => items.map((i) => (i.file ? `${i.id}:${i.file.state}` : "")).join("|"),
    [items],
  );
  const debriefData = useMissionDebrief(
    missionId,
    debriefOn,
    `${revision}|${fileStates}|${objectivesKey}`,
  );
  const headRef = useRef<HTMLDivElement>(null);
  /** The newest revision this view knows. A verdict answer sets it the moment it lands (before
   *  the render that shows it), so a verdict chained on a note save never sends a stale one. */
  const revisionRef = useRef(revision);
  useEffect(() => {
    revisionRef.current = revision;
  }, [revision]);
  const shown = useMemo(() => applyFilters(items, filters), [items, filters]);
  /** Sessions whose found-in-git items are still to come: each gets a placeholder in its group,
   *  so the gap is visible rather than silent (design decision 9). */
  const pending = useMemo(
    () => (filters.source === "delivered" ? [] : (shelf?.foundPending ?? [])),
    [shelf, filters.source],
  );
  const groups = useMemo(() => {
    if (filters.group !== "session")
      return shown.length ? [{ key: null, items: shown }] : [];
    const out = groupBySession(shown);
    for (const k of pending)
      if (!out.some((g) => g.key === k)) out.push({ key: k, items: [] });
    return out;
  }, [shown, filters.group, pending]);
  // The lightbox walks the tiles in the order they were drawn when it opened.
  const drawn = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const order = useMemo(() => {
    if (!walk) return drawn;
    const byId = new Map(items.map((i) => [i.id, i]));
    return walk.map((id) => byId.get(id)).filter((i): i is ShelfItem => !!i);
  }, [walk, drawn, items]);
  const openIndex = open ? order.findIndex((i) => i.id === open) : -1;

  const sessionLabel = useCallback(
    (key: string | null) => {
      if (!key) return "No session";
      const title = sessionTitles[key];
      return title ? `${engineOf(key)} · ${title}` : key;
    },
    [sessionTitles],
  );

  const onOpen = useCallback(
    (item: ShelfItem) => {
      opener.current = tiles.current.get(item.id) ?? null;
      setWalk(drawn.map((i) => i.id));
      setOpen(item.id);
    },
    [drawn],
  );

  const onVerdict = useCallback(
    async (
      base: ShelfItem,
      verdict: ShelfVerdict,
      note: string,
    ): Promise<VerdictResult> => {
      const attempt = async (rev: number) => {
        const r = await api.setShelfVerdict(missionId, base.id, {
          expected_revision: rev,
          verdict,
          note: note.trim(),
        });
        revisionRef.current = r.revision;
        applyVerdict(r.revision, r.item);
        return r.item;
      };
      try {
        // The LATEST revision, never the one this callback was made with: a verdict chained on a
        // note save is sent after that save moved the shelf.
        return { ok: true, item: await attempt(revisionRef.current) };
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 409) {
          return {
            ok: false,
            message:
              e instanceof ApiError
                ? e.message
                : "The verdict was not saved. Try again.",
          };
        }
      }
      // THE SHELF MOVED (an agent delivered something, or a verdict landed elsewhere). Re-read it,
      // and go again only when THIS item is still exactly what the operator was looking at — a
      // verdict another tab set on it is never overwritten without being seen.
      const fresh = await reload();
      const now = fresh?.items.find((i) => i.id === base.id);
      if (!fresh || !now)
        return {
          ok: false,
          message: "The shelf could not be re-read. Try again.",
        };
      if (now.verdict !== base.verdict || now.note !== base.note) {
        return {
          ok: false,
          message:
            "This item's verdict changed elsewhere; it shows the current one now.",
        };
      }
      try {
        return { ok: true, item: await attempt(fresh.revision) };
      } catch (e) {
        return {
          ok: false,
          message:
            e instanceof ApiError
              ? e.message
              : "The verdict was not saved. Try again.",
        };
      }
    },
    [missionId, applyVerdict, reload],
  );

  if (!shelf) {
    return (
      <div
        className={styles.view}
        data-testid="mission-shelf"
        data-state="loading"
      >
        {error ? (
          <div className={styles.notice}>{error}</div>
        ) : (
          <p className={styles.lbl}>Reading the shelf…</p>
        )}
      </div>
    );
  }

  const c = shelf.counts;
  const kindChip = (k: ShelfKind | "all") => {
    const n = k === "all" ? c.total : c.by_kind[k];
    const Icon = k === "all" ? null : KIND_ICON[k];
    return (
      <button
        key={k}
        type="button"
        className="nj-chip"
        aria-pressed={filters.kind === k}
        onClick={() => setFilters((f) => ({ ...f, kind: k }))}
        data-shelf-kind-chip={k}
      >
        {Icon ? <Icon aria-hidden="true" /> : null}
        {k === "all" ? "All" : KIND_LABEL[k]}
        <span className="nj-n">{n}</span>
      </button>
    );
  };
  const seg = <T extends string>(
    label: string,
    value: T,
    opts: [T, string, number | undefined][],
    set: (v: T) => void,
  ) => (
    <div className="nj-seg" role="radiogroup" aria-label={label}>
      {opts.map(([v, l, n]) => (
        <button
          key={v}
          type="button"
          role="radio"
          aria-checked={value === v}
          onClick={() => set(v)}
        >
          {l}
          {n !== undefined ? <span className="nj-n">{n}</span> : null}
        </button>
      ))}
    </div>
  );

  return (
    <div
      className={styles.view}
      data-testid="mission-shelf"
      data-revision={shelf.revision}
    >
      {error ? <div className={styles.notice}>{error}</div> : null}
      {debriefOn ? (
        <MissionDebrief
          missionId={missionId}
          data={debriefData}
          configured={configured}
          sessionLabel={(k) =>
            (k && sessionTitles[k]) || (k ? k.slice(k.indexOf(":") + 1, k.indexOf(":") + 9) : "")
          }
          onReview={() => {
            setFilters({ ...NO_FILTERS, unreviewedOnly: true });
            headRef.current?.scrollIntoView?.({ block: "start" });
          }}
        />
      ) : null}
      <div className={styles.head} ref={headRef}>
        <div className={styles.title}>
          <div className="nj-callsign">
            <span className="nj-notch" aria-hidden="true" />
            <span className="nj-title">The shelf</span>
            <span>//</span>
            <span className="nj-value" data-shelf-total="">
              {c.total} {c.total === 1 ? "item" : "items"}
            </span>
            <span className={styles.hideM}>//</span>
            <span className={styles.hideM}>
              {c.by_source.delivered} delivered · {c.by_source.found} found
            </span>
          </div>
          <div
            className={styles.unrev}
            aria-label={`${c.unreviewed} of ${c.total} unreviewed`}
          >
            <span className="nj-readout is-sm" data-shelf-unreviewed="">
              {c.unreviewed}
            </span>
            <span className={styles.of} aria-hidden="true">
              of {c.total}
              <br />
              unreviewed
            </span>
          </div>
        </div>
        {c.total ? (
          <div className={styles.filters}>
            <div
              className={`${styles.chipRow} ${styles.isScroll}`}
              data-shelf-kinds=""
            >
              <span className={styles.rowLbl}>Kind</span>
              {kindChip("all")}
              {KIND_ORDER.filter((k) => c.by_kind[k] > 0).map(kindChip)}
            </div>
            <div className={styles.chipRow}>
              <span className={styles.rowLbl}>Source</span>
              {seg(
                "Source",
                filters.source,
                [
                  ["all", "All", undefined],
                  ["delivered", "Delivered", c.by_source.delivered],
                  ["found", "Found", c.by_source.found],
                ],
                (v) => setFilters((f) => ({ ...f, source: v })),
              )}
              <button
                type="button"
                className="nj-chip"
                aria-pressed={filters.unreviewedOnly}
                onClick={() =>
                  setFilters((f) => ({
                    ...f,
                    unreviewedOnly: !f.unreviewedOnly,
                  }))
                }
              >
                Unreviewed only
              </button>
              <span className={styles.push} />
              {seg(
                "Group by",
                filters.group,
                [
                  ["session", "By session", undefined],
                  ["none", "Flat", undefined],
                ],
                (v) => setFilters((f) => ({ ...f, group: v })),
              )}
            </div>
          </div>
        ) : null}
      </div>

      {c.total === 0 ? (
        <section className={styles.empty} data-shelf-empty="">
          <h3>Nothing on the shelf yet</h3>
          <p>
            Results arrive here as the mission's agents deliver them: files they
            produced, links and text. What a session changes in git is found
            when it ends. Every item gets your verdict.
          </p>
          <div className={styles.kindStrip}>
            {KIND_ORDER.map((k) => {
              const Icon = KIND_ICON[k];
              return (
                <span key={k}>
                  <Icon aria-hidden="true" />
                  {KIND_LABEL[k]}
                </span>
              );
            })}
          </div>
        </section>
      ) : null}
      {c.total > 0 && shown.length === 0 && !groups.length ? (
        <section className={styles.empty}>
          <p>No item matches these filters.</p>
          <button
            type="button"
            className="nj-chip"
            onClick={() =>
              setFilters((f) => ({ ...NO_FILTERS, group: f.group }))
            }
          >
            Clear filters
          </button>
        </section>
      ) : null}
      {groups.map((g) => {
        const unrev = g.items.filter((i) => i.verdict === null).length;
        const path = g.key ? sessionPathFromKey(g.key) : null;
        const title = g.key ? (sessionTitles[g.key] ?? g.key) : "Items";
        return (
          <section
            key={g.key ?? "flat"}
            className={styles.group}
            aria-label={filters.group === "session" ? title : "All items"}
            data-shelf-group={g.key ?? ""}
          >
            {filters.group === "session" ? (
              <div className={styles.gHead}>
                <div className={styles.gTitle}>
                  {g.key ? (
                    <span className={styles.eng}>{engineOf(g.key)}</span>
                  ) : null}
                  <span>{title}</span>
                </div>
                <span className={styles.gMeta}>
                  <b>{g.items.length}</b>{" "}
                  {g.items.length === 1 ? "item" : "items"} · <b>{unrev}</b> to
                  review
                </span>
                {path ? (
                  <Link className={styles.gLink} to={path}>
                    Open session <ChevronRight size={14} aria-hidden="true" />
                  </Link>
                ) : null}
              </div>
            ) : null}
            <div className={styles.grid}>
              {g.key && pending.includes(g.key) ? (
                <div
                  className={styles.pending}
                  data-shelf-found-pending={g.key}
                >
                  <span className={styles.pendingTag}>
                    <GitBranch aria-hidden="true" />
                    Found in git
                  </span>
                  <span>
                    Collected when this session ends: what it changed in the
                    repository.
                  </span>
                </div>
              ) : null}
              {g.items.map((it) => (
                <ShelfTile
                  key={it.id}
                  ref={(el) => {
                    if (el) tiles.current.set(it.id, el);
                    else tiles.current.delete(it.id);
                  }}
                  missionId={missionId}
                  item={it}
                  onOpen={onOpen}
                />
              ))}
            </div>
          </section>
        );
      })}

      {openIndex >= 0 ? (
        <ShelfLightbox
          missionId={missionId}
          items={order}
          index={openIndex}
          sessionLabel={sessionLabel}
          onIndex={(i) => setOpen(order[i]?.id ?? null)}
          onClose={() => {
            setOpen(null);
            setWalk(null);
          }}
          onVerdict={onVerdict}
          triggerRef={opener}
        />
      ) : null}
    </div>
  );
}
