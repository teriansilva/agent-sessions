/** A review loop's fields (#2202 board 1): where it reviews, who reviews, what it reviews, and
 *  whether the verdict goes back. Only specialists on an API client are offered. Unchanged by the
 *  wizard (#2221 P2b): it is step 3's "Review loop". */
import { ROUNDS_MAX, ROUNDS_MIN, reviewerOptions, type EditorForm } from "../../../lib/automations";
import styles from "../automations.module.css";

export function ReviewLoopFields({
  form,
  set,
  f,
  projects,
  reviewers,
  reviewerChosen,
  specialistsLoaded,
}: {
  form: EditorForm;
  set: <K extends keyof EditorForm>(k: K, v: EditorForm[K]) => void;
  f: (name: string) => string;
  projects: { id: string; name: string; default_folder?: string | null }[];
  reviewers: ReturnType<typeof reviewerOptions>;
  reviewerChosen: ReturnType<typeof reviewerOptions>[number] | null;
  specialistsLoaded: boolean;
}) {
  const rounds = Array.from({ length: ROUNDS_MAX - ROUNDS_MIN + 1 }, (_, i) => i + ROUNDS_MIN);
  const newer =
    reviewerChosen && form.reviewerVersion != null && reviewerChosen.version !== form.reviewerVersion
      ? reviewerChosen
      : null;
  return (
    <div className={styles.fields} data-testid="review-loop-fields">
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-project")}>
          Project
        </label>
        <select
          id={f("loop-project")}
          className={styles.control}
          value={form.projectId}
          onChange={(e) => set("projectId", e.target.value)}
          data-testid="loop-project"
        >
          <option value="">Choose a project…</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.default_folder ? ` · ${p.default_folder}` : ""}
            </option>
          ))}
          {form.projectId && !projects.some((p) => p.id === form.projectId) && (
            <option value={form.projectId}>{form.projectId}</option>
          )}
        </select>
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-repo")}>
          Repository
        </label>
        <input
          id={f("loop-repo")}
          className={`${styles.control} ${styles.mono}`}
          value={form.repo}
          maxLength={201}
          placeholder="owner/name"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          onChange={(e) => set("repo", e.target.value)}
          data-testid="loop-repo"
        />
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-reviewer")}>
          Reviewer specialist
        </label>
        <select
          id={f("loop-reviewer")}
          className={styles.control}
          value={form.reviewerId}
          onChange={(e) => {
            const r = reviewers.find((x) => x.id === e.target.value);
            set("reviewerId", e.target.value);
            set("reviewerVersion", r ? r.version : null);
          }}
          aria-describedby={f("loop-reviewer-hint")}
          data-testid="loop-reviewer"
        >
          <option value="">{specialistsLoaded ? "Choose a specialist…" : "Loading specialists…"}</option>
          {reviewers.map((r) => (
            <option key={r.id} value={r.id} disabled={!!r.blocked}>
              {r.label}
              {r.blocked ? " — can’t start unattended" : ""}
            </option>
          ))}
          {form.reviewerId && !reviewerChosen && (
            <option value={form.reviewerId}>
              {form.reviewerId} v{form.reviewerVersion ?? "?"} — not offered now
            </option>
          )}
        </select>
        {form.reviewerId && form.reviewerVersion != null && (
          <p className={styles.hint} data-testid="loop-reviewer-pin">
            Pinned at version {form.reviewerVersion}. A newer version, or archiving it, pauses the
            loop until you approve again.
          </p>
        )}
        {newer && (
          <button
            type="button"
            className={styles.ghost}
            onClick={() => set("reviewerVersion", newer.version)}
            data-testid="loop-reviewer-repin"
          >
            Pin version {newer.version}
          </button>
        )}
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-login")}>
          Reviewer account
        </label>
        <input
          id={f("loop-login")}
          className={`${styles.control} ${styles.mono}`}
          value={form.reviewerLogin}
          maxLength={64}
          placeholder="the forge login it posts as"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          onChange={(e) => set("reviewerLogin", e.target.value)}
          data-testid="loop-login"
        />
      </div>
      <div id={f("loop-reviewer-hint")} className={`${styles.hint} ${styles.full}`}>
        Only specialists on an API client are offered: nothing unattended drives a terminal. The
        specialist posts its verdict with its own forge credentials; BattleLab only starts it and
        never writes to the forge. Only verdicts from the reviewer account are trusted.
        {specialistsLoaded && reviewers.length === 0 && (
          <strong data-testid="loop-no-reviewers"> No specialist runs on an API client yet: create one in Library → Specialists.</strong>
        )}
        {reviewers
          .filter((r) => r.blocked)
          .map((r) => (
            <span key={r.id} className={styles.sub}>
              {" "}
              {r.label}: {r.blocked}.
            </span>
          ))}
      </div>

      <div className={`${styles.field} ${styles.full}`} role="group" aria-labelledby={f("loop-legs")}>
        <span className={styles.label} id={f("loop-legs")}>
          Review
        </span>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={form.reviewIssues}
            onChange={(e) => set("reviewIssues", e.target.checked)}
            data-testid="loop-issues"
          />
          Issues — when opened, reopened or edited
        </label>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={form.reviewPulls}
            onChange={(e) => set("reviewPulls", e.target.checked)}
            data-testid="loop-pulls"
          />
          Pull requests — when opened, reopened or pushed (drafts skipped)
        </label>
      </div>

      <div className={`${styles.field} ${styles.full}`}>
        <span className={styles.label}>Return the verdict</span>
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={form.returnToOrigin}
            onChange={(e) => set("returnToOrigin", e.target.checked)}
            data-testid="loop-return"
          />
          Send the verdict to the API session that opened the issue or pull request
        </label>
        <p className={styles.hint}>A terminal session gets nothing: you are notified instead, with a link to it.</p>
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-rounds-pr")}>
          Reviews per pull request
        </label>
        <select
          id={f("loop-rounds-pr")}
          className={styles.control}
          value={form.roundsPr}
          onChange={(e) => set("roundsPr", Number(e.target.value))}
          data-testid="loop-rounds-pr"
        >
          {rounds.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label className={styles.label} htmlFor={f("loop-rounds-issue")}>
          Reviews per issue
        </label>
        <select
          id={f("loop-rounds-issue")}
          className={styles.control}
          value={form.roundsIssue}
          onChange={(e) => set("roundsIssue", Number(e.target.value))}
          data-testid="loop-rounds-issue"
        >
          {rounds.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>
      <p className={`${styles.hint} ${styles.full}`}>
        Counted over the item’s whole life. After the last one, no review starts and you get one
        notification. Each review first checks the item’s current revision on your forge (Settings →
        Forge).
      </p>
    </div>
  );
}
