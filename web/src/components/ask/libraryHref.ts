/** Where a done Library save (#1611) lives, for its card's Open in Library link. */
import {
  automationPath,
  AUTOMATIONS_PATH,
  playbookPath,
  SKILLS_PATH,
  VARIABLES_PATH,
} from "../../lib/routes";
import type { AskOperationCard } from "../../types/api";

export function libraryHref(saved: NonNullable<AskOperationCard["library"]>): string {
  if (saved.kind === "playbook") return playbookPath(saved.id);
  if (saved.kind === "skill") return `${SKILLS_PATH}/${encodeURIComponent(saved.id)}`;
  if (saved.kind === "secret") return VARIABLES_PATH;
  return saved.id ? automationPath(saved.id) : AUTOMATIONS_PATH;
}
