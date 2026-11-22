/** Library → Actions (#2248 P1): the gallery of the steps an automation can take. Read-only: one card
 *  per action with its category, effect class (pips, never a status colour), what it needs and how
 *  many automations use it. Search and category chips filter it; nothing here runs anything.
 *
 *  P1 lists today's five actions. A category with no action yet is not a chip: the page says more
 *  come in later releases instead of offering chips that filter to nothing. */
import { useState } from "react";
import { Link } from "react-router-dom";

import { ActionCard, EffectTag } from "../components/actions/ActionBits";
import { useActions } from "../components/actions/useActions";
import { LibraryGrid, LibraryHead, LibraryToolbar, type Facet } from "../components/library/LibraryGrid";
import lib from "../components/library/library.module.css";
import pb from "../components/playbooks/playbooks.module.css";
import buttons from "../components/ui/actionButton.module.css";
import styles from "../components/actions/actions.module.css";
import { categoryChips, comingCategories, visibleActions, type CategoryFilter } from "../lib/actions";
import { AUTOMATIONS_PATH } from "../lib/routes";

const INTRO =
  "The steps an automation can take. Each one says what it may change and what it needs. Nothing here runs by itself: an automation runs an action only after you approve it.";

export default function Actions() {
  const { gallery, error, loading, reload } = useActions();
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<CategoryFilter>("all");
  const chips = gallery ? categoryChips(gallery, search) : [];
  const cat = chips.some((c) => c.value === category) ? category : "all";
  const rows = gallery ? visibleActions(gallery, search, cat) : [];
  const facets: Facet<CategoryFilter>[] = chips;
  const coming = gallery ? comingCategories(gallery) : [];
  return (
    <main className={`${pb.page} ${lib.page}`} data-testid="actions-page">
      <LibraryHead section="actions" title="Actions" count={gallery?.actions.length} />
      <p className={lib.intro}>{INTRO}</p>
      <LibraryToolbar
        label="Search actions"
        placeholder="Search actions"
        search={search}
        onSearch={setSearch}
        facets={gallery ? facets : undefined}
        facet={cat}
        onFacet={setCategory}
        count={gallery ? `${rows.length} ${rows.length === 1 ? "action" : "actions"}` : undefined}
      />
      {gallery && gallery.effects.length > 0 && (
        <div className={styles.legend} data-testid="actions-legend">
          <span>What it may change, least to most:</span>
          {gallery.effects.map((e) => (
            <EffectTag key={e.id} gallery={gallery} effect={e.id} />
          ))}
        </div>
      )}
      {error && (
        <div className={lib.error} role="alert" data-testid="actions-error">
          {error}{" "}
          <button className={buttons.ghost} onClick={reload} disabled={loading}>
            {loading ? "Retrying…" : "Try again"}
          </button>
        </div>
      )}
      {loading && !gallery && !error && <p role="status">Loading actions…</p>}
      {gallery && (
        <LibraryGrid testId="actions-grid">
          {rows.map((a) => (
            <ActionCard key={a.id} gallery={gallery} action={a} />
          ))}
        </LibraryGrid>
      )}
      {gallery && rows.length === 0 && (
        <p className={lib.empty} role="status" data-testid="actions-none">
          No action matches “{search.trim()}”.
        </p>
      )}
      {gallery && (
        <p className={styles.coming} data-testid="actions-coming">
          An automation picks one of these in its <b>What</b> step (
          <Link to={AUTOMATIONS_PATH}>Automations</Link>).
          {coming.length > 0 && ` More come in later releases: ${coming.join(", ").toLowerCase()}.`}
        </p>
      )}
    </main>
  );
}
