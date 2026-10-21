/** NIGHTJAR loaders (#1531, docs/design.md v2 §6/§8). Every wait is one of four shapes, and every
 *  one names what it is waiting for in words (a `role="status"` label), so Reduced / Off — where
 *  the shapes hold still — still say "working". */
export type LoaderKind = "acquire" | "steps" | "radar" | "skeleton";

export function Loader({
  kind,
  label,
  rows = 3,
  className = "",
  testId,
}: {
  kind: LoaderKind;
  /** What is being waited for ("Acquiring sessions"). Spoken; shown beside acquire/steps/radar. */
  label: string;
  /** Skeleton only: how many rows. */
  rows?: number;
  className?: string;
  testId?: string;
}) {
  if (kind === "skeleton")
    return (
      <div className={`nj-loader is-skeleton ${className}`.trim()} role="status" data-testid={testId}>
        <span className="nj-sr-only">{label}</span>
        {Array.from({ length: rows }, (_, i) => (
          <div key={i} className="nj-skeleton" aria-hidden="true" style={{ width: `${92 - i * 14}%` }} />
        ))}
      </div>
    );
  return (
    <div
      className={`nj-loader is-${kind} ${className}`.trim()}
      role="status"
      data-testid={testId}
    >
      {kind === "acquire" && <div className="nj-acquire" aria-hidden="true" />}
      {kind === "steps" && (
        <span className="nj-steps" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      )}
      {kind === "radar" && <span className="nj-radar" aria-hidden="true" />}
      <span className="nj-loader-label">{label}</span>
    </div>
  );
}
