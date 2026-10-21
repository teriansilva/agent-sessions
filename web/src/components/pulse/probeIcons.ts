/** One glyph per probe kind (#1221), beside `PROBE_LABELS` in `judgment.ts`.
 *
 *  The checklist cards, the step rows and the kind select all read this one map, so a kind reads
 *  the same everywhere it is drawn. A kind the map does not know (a newer server) falls back to a
 *  neutral glyph rather than to nothing. Shapes only, never a status colour: a probe KIND is not a
 *  state (docs/design.md §3). */
import {
  CheckCheck,
  CircleDashed,
  Eye,
  GitBranch,
  GitMerge,
  GitPullRequest,
  Globe,
  Play,
  Radar,
  Scale,
  StickyNote,
  type LucideIcon,
} from "lucide-react";

export const PROBE_ICONS: Record<string, LucideIcon> = {
  none: StickyNote,
  supervisor_judged: Scale,
  git_local: GitBranch,
  forge_pr: GitPullRequest,
  forge_checks: CheckCheck,
  forge_review: Eye,
  forge_merged: GitMerge,
  forge_run: Play,
  http_status: Globe,
  http_revision: Radar,
};

export function probeIcon(kind: string): LucideIcon {
  return PROBE_ICONS[kind] ?? CircleDashed;
}

/** Who settles an objective of this kind: the server's probe, the supervisor's judgment, or the
 *  operator. The counts on a checklist card are this, tallied. */
export function probeSettler(kind: string): "probe" | "judged" | "you" {
  if (kind === "none") return "you";
  if (kind === "supervisor_judged") return "judged";
  return "probe";
}
