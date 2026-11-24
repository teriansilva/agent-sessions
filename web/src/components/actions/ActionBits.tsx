/** Library → Actions (#2248 P1): an action's icon, its effect tag, its gallery card and the wizard's
 *  compact picker. Every fact comes from `GET /api/actions` (the manifest); nothing here runs. */
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Crosshair,
  FileTerminal,
  MessageSquare,
  FileUp,
  NotebookPen,
  Send,
  Webhook,
  RadioTower,
  Search,
  SquareTerminal,
  Terminal,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { Chip } from "../hud/Controls";
import {
  categoryChips,
  categoryLabel,
  effectOf,
  needsSetup,
  requirementLine,
  usedByWords,
  visibleActions,
  type CategoryFilter,
} from "../../lib/actions";
import type { ActionEffect, ActionEntry, ActionGallery } from "../../types/actions";
import { settingsPath, type SettingsSectionId } from "../../routes/settingsTabs";
import lib from "../library/library.module.css";
import styles from "./actions.module.css";

/** An action's glyph, by its id. An action this build has no glyph for gets a neutral one. */
const ICON: Record<string, LucideIcon> = {
  start_mission: Crosshair,
  start_session: SquareTerminal,
  send_to_session: MessageSquare,
  tell_hq: RadioTower,
  send_message: Send,
  call_webhook: Webhook,
  publish_artifact: FileUp,
  write_memory: NotebookPen,
  run_command: Terminal,
  run_script: FileTerminal,
};

export function ActionIcon({ id }: { id: string }) {
  const Icon = ICON[id] ?? Zap;
  return (
    <span className={styles.icobox} aria-hidden="true">
      <Icon size={18} />
    </span>
  );
}

/** How far an action reaches: pips (1–5, least to most) and its words. A class, never a state. */
export function EffectTag({ gallery, effect }: { gallery: ActionGallery | null; effect: ActionEffect }) {
  const { level, label } = effectOf(gallery, effect);
  return (
    <span
      className={styles.fx}
      data-effect={effect}
      data-testid="action-effect"
      title={`What it may change: ${label} (${level} of 5)`}
    >
      <span className={styles.pips} aria-hidden="true">
        {[1, 2, 3, 4, 5].map((i) => (
          <i key={i} className={i <= level ? styles.on : undefined} />
        ))}
      </span>
      {label}
    </span>
  );
}

/** One gallery card: category and effect, the name, its line, and what it needs with its use. */
export function ActionCard({ gallery, action }: { gallery: ActionGallery; action: ActionEntry }) {
  const req = requirementLine(action);
  return (
    <article
      className={`${lib.card} ${req.tone === "degraded" ? styles.needs : ""}`.trim()}
      data-testid="action-card"
      data-id={action.id}
    >
      <div className={`${lib.top} ${styles.top}`}>
        <span className={styles.cat}>{categoryLabel(gallery, action.category)}</span>
        <EffectTag gallery={gallery} effect={action.effect} />
      </div>
      <div className={styles.name}>
        <ActionIcon id={action.id} />
        <h2 className={lib.title}>{action.label}</h2>
      </div>
      <p className={`${lib.sum} ${styles.full}`}>{action.description}</p>
      <div className={lib.foot}>
        <span className={`${styles.state} ${styles[req.tone]}`} data-testid="action-requires">
          <span className={`hud-led ${req.tone === "up" ? "up" : "att"}`} aria-hidden="true" />
          <b>{req.word}</b>
          {req.extra && <span>· {req.extra}</span>}
        </span>
        <span className={styles.used} data-testid="action-used-by">
          {usedByWords(action.used_by)}
        </span>
      </div>
    </article>
  );
}

/** The automation wizard's What step (#2248 P1): a compact, searchable list of the actions the
 *  trigger can start, from the same gallery. The search and the chips sit OUTSIDE the option group,
 *  so the group holds the actions and nothing else; the picked one stays listed whatever the search
 *  says. Picking is what it was: one press sets the action. */
export function ActionPicker({
  gallery,
  offered,
  value,
  onChange,
  testId,
}: {
  gallery: ActionGallery;
  offered: ActionEntry[];
  value: string;
  onChange: (id: string) => void;
  testId: string;
}) {
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<CategoryFilter>("all");
  const shown = { ...gallery, actions: offered };
  const chips = categoryChips(shown, search);
  const cat = chips.some((c) => c.value === category) ? category : "all";
  const visible = new Set(visibleActions(shown, search, cat).map((a) => a.id));
  const rows = offered.filter((a) => visible.has(a.id) || a.id === value);
  return (
    <div className={styles.picker}>
      <span className={styles.pickSearch}>
        <Search size={16} aria-hidden />
        <input
          className="nj-field"
          type="search"
          aria-label="Search actions"
          placeholder="Search actions"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          data-testid={`${testId}-search`}
        />
      </span>
      {chips.length > 2 && (
        <div className={styles.pickChips} role="group" aria-label="Category" data-testid={`${testId}-chips`}>
          {chips.map((c) => (
            <Chip key={c.value} on={cat === c.value} onClick={() => setCategory(c.value)}>
              {c.label}
            </Chip>
          ))}
        </div>
      )}
      <div className={styles.rows} role="group" aria-label="What it does" data-testid={testId}>
        {rows.map((a) => {
          const req = requirementLine(a);
          const setup = value === a.id ? null : needsSetup(a);
          if (setup)
            // The needs-setup state (#2248 P2): a requirement checked now does not hold, so the
            // action can't be picked; the row says why and links to where it is set up.
            return (
              <div
                key={a.id}
                className={`${styles.row} ${styles.rowBlocked}`}
                aria-disabled="true"
                data-value={a.id}
                data-testid={`${testId}-${a.id}`}
                data-state="needs-setup"
              >
                <ActionIcon id={a.id} />
                <span className={styles.rowText}>
                  <span className={styles.rowName}>{a.label}</span>
                  <span className={styles.rowNeeds} data-testid="action-needs">
                    {setup.words}
                  </span>
                </span>
                <span className={styles.rowRight}>
                  {setup.setup ? (
                    <Link
                      className={styles.setup}
                      to={settingsPath(setup.setup as SettingsSectionId)}
                      data-testid="action-setup"
                      aria-label={`Set up ${a.label}`}
                    >
                      <span className={styles.setupLed} aria-hidden="true" />
                      Set up
                    </Link>
                  ) : (
                    <EffectTag gallery={gallery} effect={a.effect} />
                  )}
                </span>
              </div>
            );
          return (
            <button
              key={a.id}
              type="button"
              className={styles.row}
              aria-pressed={value === a.id}
              onClick={() => onChange(a.id)}
              data-value={a.id}
              data-testid={`${testId}-${a.id}`}
            >
              <ActionIcon id={a.id} />
              <span className={styles.rowText}>
                <span className={styles.rowName}>{a.label}</span>
                <span className={styles.rowLine}>{a.description}</span>
                {req.tone === "degraded" && <span className={styles.rowNeeds}>{req.word}</span>}
              </span>
              <span className={styles.rowRight}>
                <EffectTag gallery={gallery} effect={a.effect} />
              </span>
            </button>
          );
        })}
      </div>
      {rows.length === 0 && (
        <p className={styles.none} role="status">
          No action matches “{search.trim()}”.
        </p>
      )}
    </div>
  );
}
