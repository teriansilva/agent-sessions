import { Check, X } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import type { Filters } from "../../hooks/useSessionsList";
import { projectColor } from "../../lib/format";
import {
  addsPick,
  parentLookup,
  removePick,
  selectionOf,
  selectionPatch,
  toggleCluster,
  toggleProject,
  type ScopeSelection,
} from "../../lib/scopeSelection";
import {
  clusterName,
  clusterProjectName,
  projectName,
  type ScopeFacets,
} from "./scopeNames";
import styles from "./ScopeTags.module.css";

interface Props {
  filters: Filters;
  facets: ScopeFacets;
  /** Session clusters are on (#1997): clusters are offered and named. */
  clustersOn: boolean;
  onChange: (patch: Partial<Filters>) => void;
}

interface Option {
  kind: "project" | "cluster";
  id: string;
  label: string;
  /** The cluster's project, shown before its title. */
  parent?: string;
  color?: string;
  count?: number;
}

const fold = (s: string) => s.toLocaleLowerCase();

/** The project/cluster field as a tag field (#2438): every pick is a chip with its own ×, and an
 *  ARIA combobox adds or removes picks by name. Typing filters the facets the list already has, in
 *  the browser; nothing is asked of the server until a pick changes.
 *
 *  The suggestions are an in-flow listbox under the input, never a sheet or an overlay: they are
 *  read while a phone's on-screen keyboard is up, and a bottom sheet would sit under it (the same
 *  reason the pane's own menus stay anchored, #958/#1005). Focus stays in the input
 *  (`aria-activedescendant`); ↑/↓ move, Enter picks or unpicks, Esc closes, Backspace on an empty
 *  input removes the last chip. */
export function ScopeTags({ filters, facets, clustersOn, onChange }: Props) {
  const sel = selectionOf(filters);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  // A press outside while the list is open: the blur it causes must not close the list yet.
  const pressOutside = useRef(false);
  const captionId = useId();
  const listId = useId();
  // The last title seen per cluster: a chip keeps its name while the cluster store is unreadable.
  // Kept in state and updated during render when the facets change (React's "store information
  // from previous renders" pattern), never in a ref read while rendering.
  const [titles, setTitles] = useState<Record<string, string>>(() =>
    Object.fromEntries((facets.clusters ?? []).map((c) => [c.id, c.title])),
  );
  const [seenClusters, setSeenClusters] = useState(facets.clusters);
  if (facets.clusters !== seenClusters) {
    setSeenClusters(facets.clusters);
    if (facets.clusters) {
      const next = { ...titles };
      for (const c of facets.clusters) next[c.id] = c.title;
      setTitles(next);
    }
  }

  const parentOf = parentLookup(facets.clusters, filters.clusterParents);
  // Adding a tag clears a mission single choice, as the tree does; a removal leaves it (#2438).
  const apply = (next: ScopeSelection) =>
    onChange({
      ...selectionPatch(next, parentOf),
      ...(filters.mission && addsPick(sel, next) ? { mission: "" } : {}),
    });

  const options = useMemo<Option[]>(() => {
    const projectOpts: Option[] = facets.projects.map((p) => ({
      kind: "project",
      id: p.id,
      label: p.name,
      color: p.color || projectColor(p.id),
      count: p.count,
    }));
    const clusterOpts: Option[] = clustersOn
      ? (facets.clusters ?? []).map((c) => ({
          kind: "cluster",
          id: c.id,
          label: c.title,
          parent: facets.projects.find((p) => p.id === c.project_id)?.name,
          count: c.count,
        }))
      : [];
    const q = fold(query.trim());
    return [...projectOpts, ...clusterOpts].filter(
      (o) =>
        !q || fold(o.label).includes(q) || fold(o.parent ?? "").includes(q),
    );
  }, [facets.projects, facets.clusters, clustersOn, query]);

  const picked = (o: Option) =>
    !sel.unclustered &&
    (o.kind === "project" ? sel.projects : sel.clusters).includes(o.id);
  const pick = (o: Option) => {
    apply(
      o.kind === "project"
        ? toggleProject(sel, o.id, parentOf)
        : toggleCluster(sel, o.id, parentOf),
    );
    // Focus stays in the input: an option takes the press on mousedown and prevents the blur.
    setQuery("");
    setActive(0);
  };
  const remove = (id: string) => {
    apply(removePick(sel, id));
    inputRef.current?.focus();
  };

  const chips: { id: string; node: ReactNode; label: string }[] = [];
  if (sel.unclustered) {
    const name = sel.projects[0] ? projectName(facets, sel.projects[0]) : "";
    const label = name ? `${name} › Not clustered` : "Not clustered";
    chips.push({
      id: sel.projects[0] ?? "none",
      label,
      node: (
        <>
          <span className={styles.bulletOpen} aria-hidden="true" />
          <span className={styles.chipText}>
            {name ? <span className={styles.parent}>{name} › </span> : null}
            Not clustered
          </span>
        </>
      ),
    });
  } else {
    for (const id of sel.projects) {
      const label = projectName(facets, id);
      chips.push({
        id,
        label,
        node: (
          <>
            <span
              className={styles.swatch}
              style={{
                background:
                  facets.projects.find((p) => p.id === id)?.color ||
                  projectColor(id),
              }}
              aria-hidden="true"
            />
            <span className={styles.chipText}>{label}</span>
          </>
        ),
      });
    }
    for (const id of sel.clusters) {
      const label = clusterName(facets, id, titles[id]);
      const parent = clusterProjectName(facets, id);
      chips.push({
        id,
        label: parent ? `${parent} › ${label}` : label,
        node: (
          <>
            <span className={styles.bullet} aria-hidden="true" />
            {/* The whole breadcrumb is ONE shrinkable label: a long project name truncates with
                the title, never pushing the × out of the chip (#2438 review). */}
            <span className={styles.chipText}>
              {parent ? (
                <span className={styles.parent}>{parent} › </span>
              ) : null}
              {label}
            </span>
          </>
        ),
      });
    }
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      if (!options.length) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + options.length) % options.length);
    } else if (e.key === "Enter") {
      const o = options[active];
      if (open && o) {
        e.preventDefault();
        pick(o);
      }
    } else if (e.key === "Escape") {
      if (open) {
        e.preventDefault();
        setOpen(false);
      }
    } else if (e.key === "Backspace" && query === "" && chips.length) {
      e.preventDefault();
      remove(chips[chips.length - 1].id);
    }
  };

  const showList = open;
  const activeId =
    showList && options[active] ? `${listId}-${active}` : undefined;
  // The list is in the flow, so closing it moves what is under it. A press outside (on a tree node,
  // say) blurs the input on mousedown; closing then would shift the tree up before mouseup and the
  // click would land on another node. So a pointer press outside closes the list only after its
  // click has been handled; a keyboard blur (Tab) closes it at once (`onBlur`).
  useEffect(() => {
    if (!open) return;
    const close = () => {
      pressOutside.current = false;
      setOpen(false);
    };
    const onDown = (e: PointerEvent) => {
      if (wrapRef.current?.contains(e.target as Node)) return;
      pressOutside.current = true;
      const after = () => {
        window.removeEventListener("click", after, true);
        window.removeEventListener("pointercancel", after, true);
        window.setTimeout(close, 0);
      };
      window.addEventListener("click", after, true);
      window.addEventListener("pointercancel", after, true);
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [open]);
  // The active option stays in view as the keys move it — on a phone the on-screen keyboard leaves
  // little room under the field, and the drawer scrolls rather than the listbox overlaying.
  useEffect(() => {
    if (activeId)
      document.getElementById(activeId)?.scrollIntoView?.({ block: "nearest" });
  }, [activeId]);
  return (
    <div className={styles.wrap} ref={wrapRef} data-testid="scope-tags">
      <div className={styles.caption} id={captionId}>
        Projects &amp; clusters
      </div>
      <div
        className={styles.field}
        role="group"
        aria-labelledby={captionId}
        onClick={(e) => {
          if (e.target === e.currentTarget) inputRef.current?.focus();
        }}
      >
        {chips.map((c) => (
          <span key={c.id} className={styles.chip} data-testid="scope-chip">
            {c.node}
            <button
              type="button"
              className={styles.x}
              aria-label={`Remove ${c.label}`}
              onClick={() => remove(c.id)}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          className={styles.input}
          type="text"
          role="combobox"
          aria-label="Add a project or cluster"
          aria-expanded={showList}
          aria-controls={showList ? listId : undefined}
          aria-autocomplete="list"
          aria-activedescendant={activeId}
          placeholder={chips.length ? "Add…" : "All projects — add…"}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            if (!pressOutside.current) setOpen(false);
          }}
          onKeyDown={onKey}
        />
      </div>
      {/* Rendered only while open: a listbox in the document, even a hidden one, reads as a surface
          open over the page (HQ's Esc stands down for it, `ask/escOwner.ts`). */}
      {showList ? (
        <ul
          id={listId}
          role="listbox"
          aria-label="Projects and clusters"
          aria-multiselectable="true"
          className={styles.list}
        >
          {options.length === 0 ? (
            <li className={styles.empty} role="presentation">
              {`No project or cluster matches “${query.trim()}”.`}
            </li>
          ) : (
            options.map((o, i) => (
              <li
                key={`${o.kind}:${o.id}`}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={picked(o)}
                className={`${styles.option} ${i === active ? styles.activeOpt : ""}`}
                // mousedown, not click: the input must keep focus (a click blurs it first).
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(o);
                }}
                onMouseEnter={() => setActive(i)}
              >
                {o.kind === "project" ? (
                  <span
                    className={styles.swatch}
                    style={{ background: o.color }}
                    aria-hidden="true"
                  />
                ) : (
                  <span className={styles.bullet} aria-hidden="true" />
                )}
                <span className={styles.optText}>
                  {o.parent ? (
                    <span className={styles.parent}>{o.parent} › </span>
                  ) : null}
                  {o.label}
                </span>
                {o.count != null ? (
                  <span className={styles.count}>{o.count}</span>
                ) : null}
                <span className={styles.check} aria-hidden="true">
                  {picked(o) ? <Check size={12} /> : null}
                </span>
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}
