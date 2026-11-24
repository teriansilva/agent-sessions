/** Subagent sessions in the sidebar (#2325, `docs/invariants/session-lineage.md`).
 *
 *  The list carries MAIN sessions only. A main session with subagents gets a disclosure chip
 *  (count + rolled-up LED); opening it loads one level of children lazily, paged. A main session
 *  whose subagents need the operator opens by itself and pins those first (the attention route
 *  lists every one, any depth, with its breadcrumb) — in `hidden` mode too, where they are the
 *  only subagent rows shown. Rows are rendered by the list's own `renderRow`, so a subagent row
 *  has the same link, status dot and ⋯ menu as any other. */
import { Fragment, useState, type ReactNode } from "react";
import type { Session, SessionRollup, SessionsPage } from "../../types/api";
import { Led } from "../hud/Meters";
import { Seg } from "../hud/Controls";
import type { SubagentMode } from "./subagentMode";
import styles from "./Subagents.module.css";
import { useLineagePages } from "./useLineagePages";

/** Filtered-out main sessions the attention strip opens per step (each one reads its rows). */
export const OUTSIDE_PAGE = 20;

export type RenderRow = (s: Session, depth: number, crumb?: string) => ReactNode;

const ROLLUP_TONE: Record<SessionRollup, "att" | "up" | "draft" | "idle"> = {
  attention: "att",
  working: "up",
  draft: "draft",
  idle: "idle",
};

const ROLLUP_WORD: Record<SessionRollup, string> = {
  attention: "one needs you",
  working: "working",
  draft: "unsent draft",
  idle: "idle",
};

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The disclosure chip on a main session row: `▸ N` + the rolled-up LED. A real button in
 *  `nested` mode; in `hidden` mode a count only (the attention rows still show by themselves). */
export function SubagentChip({
  s,
  open,
  mode,
  onToggle,
}: {
  s: Session;
  open: boolean;
  mode: SubagentMode;
  onToggle: () => void;
}) {
  const n = s.child_count ?? 0;
  if (n <= 0) return null;
  const roll = s.rollup ?? "idle";
  const state = s.attention_count
    ? `${plural(s.attention_count, "needs", "need")} you`
    : ROLLUP_WORD[roll];
  const label = `${plural(n, "subagent")}, ${state}`;
  const led = <Led tone={ROLLUP_TONE[roll]} className={styles.chipLed} />;
  if (mode === "hidden") {
    return (
      <span className={styles.chip} aria-label={`${label} (hidden)`} role="img" data-testid="subagent-count">
        {n}
        {led}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`${styles.chip} ${open ? styles.chipOpen : ""}`}
      aria-expanded={open}
      aria-label={`${open ? "Hide" : "Show"} ${label}`}
      title={label}
      onClick={onToggle}
      data-testid="subagent-toggle"
    >
      <span className={styles.caret} aria-hidden="true">
        {open ? "▾" : "▸"}
      </span>
      {n}
      {led}
    </button>
  );
}

function crumbOf(s: Session, mainId: string): string | undefined {
  const path = (s.path ?? []).filter((p) => p.id !== mainId);
  if (!path.length) return undefined;
  return `↳ ${path.map((p) => p.title || "(untitled)").join(" › ")}`;
}

/** The subagent rows under one main session (or under a subagent that has its own). */
export function SubagentRows({
  main,
  open,
  mode,
  archived,
  depth,
  renderRow,
}: {
  main: Session;
  open: boolean;
  mode: SubagentMode;
  archived: boolean;
  depth: number;
  renderRow: RenderRow;
}) {
  const wantAttention = (main.attention_count ?? 0) > 0 && (open || mode === "hidden");
  const wantChildren = open && mode === "nested";
  const version = `${main.child_count ?? 0}:${main.attention_count ?? 0}:${main.rollup ?? ""}`;
  const att = useLineagePages(main.id, "attention", wantAttention, archived, version);
  const kids = useLineagePages(main.id, "children", wantChildren, archived, version);
  if (!wantAttention && !wantChildren) return null;
  const pinned = new Set(att.items.map((s) => s.id));
  const rest = kids.items.filter((s) => !pinned.has(s.id));
  const nothing =
    !att.loading && !kids.loading && !att.error && !kids.error && !att.items.length && !rest.length;
  return (
    <li className={styles.group} data-depth={Math.min(depth, 2)}>
      <ul
        className={styles.list}
        aria-label={`Subagents of ${main.title || "(untitled)"}`}
        data-testid="subagent-group"
      >
        {att.items.map((s) => (
          <Fragment key={`a:${s.id}`}>{renderRow(s, depth, crumbOf(s, main.id))}</Fragment>
        ))}
        {att.next != null && (
          <li className={styles.note}>
            <button type="button" className={styles.moreBtn} onClick={att.more} disabled={att.loading}>
              +{att.total - att.items.length} more need you
            </button>
          </li>
        )}
        {wantChildren &&
          rest.map((s) => <Fragment key={`c:${s.id}`}>{renderRow(s, depth)}</Fragment>)}
        {wantChildren && kids.next != null && (
          <li className={styles.note}>
            <button type="button" className={styles.moreBtn} onClick={kids.more} disabled={kids.loading}>
              More subagents ({kids.total - kids.items.length})
            </button>
          </li>
        )}
        {(att.loading || kids.loading) && !att.items.length && !kids.items.length && (
          <li className={styles.note} role="status">
            Loading subagents…
          </li>
        )}
        {(att.error || kids.error) && (
          <li className={styles.note} role="alert">
            Couldn&apos;t load subagents.{" "}
            <button
              type="button"
              className={styles.retryBtn}
              onClick={() => {
                if (att.error) att.retry();
                if (kids.error) kids.retry();
              }}
            >
              Retry
            </button>
          </li>
        )}
        {nothing && (
          <li className={styles.note} data-testid="subagent-empty">
            No subagents listed right now.
          </li>
        )}
      </ul>
    </li>
  );
}

/** "N sessions need you outside this filter" (#2325, attention guarantee 3). A subagent never
 *  matches a filter; when its main session is filtered out, this strip says so and lists them on
 *  demand, opening each straight from the list. */
export function AttentionOutside({
  data,
  archived,
  renderRow,
}: {
  data: SessionsPage["attention_outside"] | null;
  archived: boolean;
  renderRow: RenderRow;
}) {
  const [show, setShow] = useState(false);
  const [shown, setShown] = useState(OUTSIDE_PAGE);
  if (!data || data.count <= 0) return null;
  const rest = data.mains.length - shown;
  return (
    <div className={styles.outside} data-testid="attention-outside">
      <button
        type="button"
        className={styles.outsideBtn}
        aria-expanded={show}
        onClick={() => setShow((v) => !v)}
      >
        <Led tone="att" />
        <span className={styles.outsideText}>
          {plural(data.count, "session")} {data.count === 1 ? "needs" : "need"} you outside this
          filter
        </span>
        <span className={styles.outsideAction}>{show ? "HIDE" : "SHOW"}</span>
      </button>
      {show && (
        <ul className={styles.list} aria-label="Sessions that need you outside this filter">
          {data.mains.slice(0, shown).map((m) => (
            <OutsideMain key={m.id} main={m} archived={archived} renderRow={renderRow} />
          ))}
          {rest > 0 && (
            <li className={styles.note}>
              <button
                type="button"
                className={styles.moreBtn}
                onClick={() => setShown((n) => n + OUTSIDE_PAGE)}
              >
                More sessions that need you ({rest} more main {rest === 1 ? "session" : "sessions"})
              </button>
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

function OutsideMain({
  main,
  archived,
  renderRow,
}: {
  main: { id: string; title: string; attention_count: number };
  archived: boolean;
  renderRow: RenderRow;
}) {
  const att = useLineagePages(main.id, "attention", true, archived, String(main.attention_count));
  return (
    <>
      {att.items.map((s) => (
        <Fragment key={s.id}>
          {renderRow(s, 1, `↳ ${[main.title || "(untitled)", ...crumbParts(s, main.id)].join(" › ")}`)}
        </Fragment>
      ))}
      {att.next != null && (
        <li className={styles.note}>
          <button type="button" className={styles.moreBtn} onClick={att.more} disabled={att.loading}>
            +{att.total - att.items.length} more need you
          </button>
        </li>
      )}
      {att.loading && !att.items.length && (
        <li className={styles.note} role="status">
          Loading…
        </li>
      )}
      {att.error && (
        <li className={styles.note} role="alert">
          Couldn&apos;t load.{" "}
          <button type="button" className={styles.retryBtn} onClick={att.retry}>
            Retry
          </button>
        </li>
      )}
    </>
  );
}

function crumbParts(s: Session, mainId: string): string[] {
  return (s.path ?? []).filter((p) => p.id !== mainId).map((p) => p.title || "(untitled)");
}

/** The nested / hidden display toggle. */
export function SubagentModeToggle({
  mode,
  onChange,
}: {
  mode: SubagentMode;
  onChange: (m: SubagentMode) => void;
}) {
  return (
    <div className={styles.modeRow}>
      <span className={styles.modeLabel} id="subagent-mode-label">
        Subagents
      </span>
      <Seg
        options={[
          { value: "nested", label: "nested" },
          { value: "hidden", label: "hidden" },
        ]}
        value={mode}
        onChange={onChange}
        label="Show subagent sessions"
        testId="subagent-mode"
      />
    </div>
  );
}
