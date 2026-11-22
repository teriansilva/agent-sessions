/** Drive the New session form's pickers (#1436). They are native radios drawn as tiles and rows —
 *  the radio itself is visually hidden, so a spec clicks its LABEL (the tile the operator sees) and
 *  reads the radio's checked state. Picked by VALUE (an engine or project id), the way the old
 *  `<select>`s were, so a spec never depends on a display label. */
import type { Page } from "@playwright/test";

function picker(page: Page, name: "new-session-agent" | "new-session-project") {
  return {
    /** The radio for `value` — assert `toBeChecked()` / `toBeDisabled()` on it. */
    radio: (value: string) => page.locator(`input[name="${name}"][value="${value}"]`),
    /** Choose `value` by clicking its tile/row, as a person would. */
    selectOption: (value: string) =>
      page.locator(`label:has(> input[name="${name}"][value="${value}"])`).click(),
  };
}

/** The agent picker is one tile per agent and, under them, the selected agent's mode (API /
 *  Terminal) — whose radio value is the client id. `selectOption(id)` taps the agent's tile first
 *  when that client's mode is not on screen, then the mode itself (an explicit pick). */
export const agentPicker = (page: Page) => {
  const modes = picker(page, "new-session-agent");
  return {
    radio: modes.radio,
    selectOption: async (value: string) => {
      if ((await modes.radio(value).count()) === 0)
        await page.locator(`label[data-clients~="${value}"]:has(> input[name="new-session-family"])`).click();
      await modes.selectOption(value);
    },
  };
};
export const projectPicker = (page: Page) => picker(page, "new-session-project");
/** The resolved launch folder. The folder card is gone (#1458) — the project owns the folder and
 *  the start summary names it, carrying the full path as `data-cwd`. */
export const launchFolder = (page: Page) => page.getByTestId("new-session-summary");
