import type { NeedsYouAction, NeedsYouKind, NeedsYouRow, ScreenMenu } from "../../types/api";

/** What each kind is called on a row. The kind is read off the SCREEN server-side (#1086). */
export const KIND_LABEL: Record<NeedsYouKind, string> = {
  choice: "Choice",
  approval: "Approval",
  question: "Question",
  needs_inspection: "Needs a look",
};

/** The verbs whose TEXT an operator may edit before approving (server: `EDITABLE_VERBS`). */
export const TEXT_VERBS = new Set(["answer", "continue"]);

const SHORT_OPTION = 18;

/** The label of option `n` in `menu`, or null. Agent text: only ever rendered as text. */
export function optionLabel(menu: ScreenMenu | null | undefined, n: number | undefined) {
  if (!menu || typeof n !== "number") return null;
  return menu.options.find((o) => o.n === n)?.label ?? null;
}

/** What the row's Approve button says — it always NAMES what it does, or there is no button.
 *
 *  - `choose` → "Approve · <option label>" (or "option N" when the label is long or unknown);
 *  - `answer` / `continue` → "Approve · send" (the text is in the details);
 *  - anything else, or a decision the projection does not let us approve → no button. An
 *    escalation at a menu offers its options in the details, never a guessed single button. */
export function approveLabel(action: NeedsYouAction | null, menu: ScreenMenu | null) {
  if (!action || !action.can_approve) return null;
  if (action.verb === "choose" && typeof action.option === "number") {
    const label = optionLabel(action.menu ?? menu, action.option);
    return label && label.length <= SHORT_OPTION
      ? `Approve · ${label}`
      : `Approve · option ${action.option}`;
  }
  if (TEXT_VERBS.has(action.verb)) return "Approve · send";
  return null;
}

/** `engine:uuid` → `/s/:engine/:uuid`, both halves encoded (the AskConsole rule). */
export function sessionRoute(key: string): string {
  const i = key.indexOf(":");
  const engine = i < 0 ? key : key.slice(0, i);
  const uuid = i < 0 ? "" : key.slice(i + 1);
  return `/s/${encodeURIComponent(engine)}/${encodeURIComponent(uuid)}`;
}

/** The ids on the NEEDS YOU list — what the answer rows mark after asking. */
export function needsYouIds(rows: NeedsYouRow[] | undefined): Set<string> {
  return new Set((rows ?? []).map((r) => r.id));
}
