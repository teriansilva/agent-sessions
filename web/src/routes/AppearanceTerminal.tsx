import { useMemo, useState } from "react";
import { onRadioGroupKeyDown, rovingTabIndex } from "../lib/radioArrows";
import { isFontAvailable } from "../theme/fontAvailable";
import {
  coerceTermFontFamily,
  DEFAULT_TERM_FONT_FAMILY,
  presetForStack,
  TERM_FONT_FAMILY_MAX_LEN,
  TERM_FONT_PRESETS,
} from "../theme/termFont";
import { useTermFont } from "../theme/termFontStore";
import {
  DEFAULT_TERM_FONT_SIZE,
  stepTermFontSize,
  TERM_FONT_SIZE_MAX,
  TERM_FONT_SIZE_MIN,
} from "../theme/termSize";
import { useTermSize } from "../theme/termSizeStore";
import { useTermScheme } from "../theme/termSchemeStore";
import { lowContrast, resolveTermScheme, TERM_SCHEMES } from "../theme/termSchemes";
import { useTheme } from "../theme/themeStore";
import a from "./Appearance.module.css";
import styles from "./Settings.module.css";

/** Settings → Appearance → Terminal sessions (#1494). Everything about how the terminal looks —
 *  every CLI agent and the plain shell share ONE terminal, so these are single choices, saved
 *  per device. The font and size controls moved here unchanged from the old Appearance page;
 *  their invariants (docs/invariants/terminal-text-size.md, #866) are untouched. */
export function AppearanceTerminal() {
  const { theme } = useTheme();
  const { scheme, setScheme } = useTermScheme();
  const { size: termFontSize, setSize: setTermFontSize } = useTermSize();
  const { family: termFontFamily, setFamily: setTermFontFamily } = useTermFont();
  // Availability is measured ONCE per mount, not per render: the answer cannot change without
  // a page reload, and the probe writes to a canvas. Asked about each preset's PRIMARY family
  // — never its stack, which always resolves because every stack ends in `monospace` (#866).
  const fontAvailability = useMemo(
    () =>
      new Map(
        TERM_FONT_PRESETS.map((f) => [
          f.id,
          f.primary === null ? true : isFontAvailable(f.primary),
        ]),
      ),
    [],
  );
  // Draft for the custom stack, committed on Enter/blur — the same shape as the accent hex
  // field on the Interface page, and for the same reason: a stack is invalid for most of the time it is being
  // typed, and neither the live terminal nor the server should follow those keystrokes.
  const activePreset = presetForStack(termFontFamily);
  const [fontDraft, setFontDraft] = useState(
    activePreset ? "" : termFontFamily,
  );
  const [fontDraftError, setFontDraftError] = useState("");
  const [customOpen, setCustomOpen] = useState(!activePreset);
  // …and RECONCILE when the family changes from outside this component, which is not an edge
  // case: <ConfigProvider> renders children before /api/config resolves, so on a device with no
  // local choice this panel mounts on the default System stack and TermFontProvider seeds the
  // server's value a moment later. Without this, `customOpen` and `fontDraft` keep describing
  // the *initial* value: a server-seeded custom stack leaves every radio unchecked and the
  // Custom field closed, so the face that is actually live is neither shown nor editable.
  //
  // Render-phase "adjust state on change" (React's own pattern, used by syncedAccent /
  // syncedCompose above) rather than an effect: no extra commit, and no flash of the wrong
  // selection. An in-progress draft is NOT clobbered — `fontDraftDirty` means the operator is
  // mid-edit, and a late seed must not delete what they are typing.
  const [fontDraftDirty, setFontDraftDirty] = useState(false);
  const [syncedFamily, setSyncedFamily] = useState(termFontFamily);
  if (termFontFamily !== syncedFamily) {
    const preset = presetForStack(termFontFamily);
    if (fontDraftDirty) {
      // Mid-edit: touch NOTHING. Protecting only the draft text is not enough — the earlier
      // version still ran `setCustomOpen(!preset)`, so a late seed carrying a PRESET (the
      // common case: another device last chose Fira Code) unmounted the input under the
      // operator's cursor. The text survived and reappeared on reopening, which made it look
      // harmless; the interrupted editing surface and lost focus are the actual defect.
      // Caught in review, and the first delayed-seed regression could not see it because it
      // seeds a CUSTOM stack, which leaves `customOpen` true either way.
    } else {
      // Consuming the change is part of RECONCILING it, so the mark moves here and nowhere
      // else. Marking it synchronized up front (as the previous version did) threw the pending
      // family away while the dirty branch was deliberately ignoring it: empty the field, let a
      // preset seed land, then blur to cancel — `commitFontDraft` clears the dirty flag and
      // returns without touching the family, and because the change was already marked
      // consumed, no later render ever reconciled it. The terminal ran Fira Code while the
      // picker went on claiming Custom with an empty editor, permanently. Left here, the
      // difference is only WHEN: the comparison stays true while the operator types, costs
      // nothing (the dirty branch sets no state, so there is no render loop), and fires on the
      // first render after the draft is committed or cancelled.
      setSyncedFamily(termFontFamily);
      setCustomOpen(!preset);
      setFontDraft(preset ? "" : termFontFamily);
      setFontDraftError("");
    }
  }
  // Choosing a preset closes the custom field and clears its draft: the card grid and the input
  // are two views of ONE value, so leaving a stale draft behind would make the next Enter
  // silently overwrite the preset the operator just picked.
  const choosePreset = (stack: string) => {
    setTermFontFamily(stack);
    setFontDraft("");
    setFontDraftError("");
    setFontDraftDirty(false);
    setCustomOpen(false);
  };
  const commitFontDraft = () => {
    const raw = fontDraft.trim();
    if (!raw) {
      // An emptied field means "never mind", not "reset to default" — leaving the current face
      // alone is the answer that can't lose the operator's choice to a stray Backspace.
      setFontDraftError("");
      setFontDraft("");
      setFontDraftDirty(false);
      return;
    }
    // coerce is the READ boundary and returns the DEFAULT for anything unusable, so comparing
    // against it is how an invalid stack is detected — except when the operator genuinely typed
    // the default, which is legal and must not be reported as an error.
    const norm = coerceTermFontFamily(raw);
    if (norm !== raw && raw !== DEFAULT_TERM_FONT_FAMILY) {
      setFontDraftError(
        raw.length > TERM_FONT_FAMILY_MAX_LEN
          ? `Too long — ${TERM_FONT_FAMILY_MAX_LEN} characters max.`
          : "Not a usable font stack. Check for an unclosed quote, an empty name between commas, or a character other than letters, digits, spaces, commas, hyphens, dots and quotes.",
      );
      return;
    }
    setFontDraftError("");
    setFontDraft(norm);
    setFontDraftDirty(false);
    setTermFontFamily(norm);
  };

  return (
    <section className={styles.section} aria-labelledby="terminal-appearance-h">
      <h2 id="terminal-appearance-h">Terminal sessions</h2>
      <p className={styles.hint}>
        The look of every terminal. Every CLI agent and the plain shell share one terminal, so
        each of these is one choice, saved per device.
      </p>

      <h3 className={styles.subhead} id="termscheme-h">
        Colour scheme
      </h3>
      <p className={styles.hint}>
        The ground, the text and the 16 ANSI colours agents paint with. &ldquo;Follows mode&rdquo;
        schemes switch between their dark and light variants with Interface &rarr; Mode. The
        cursor always takes your accent.
      </p>
      <div
        className={a.schemes}
        role="radiogroup"
        aria-labelledby="termscheme-h"
        onKeyDown={onRadioGroupKeyDown}
      >
        {TERM_SCHEMES.map((sch, i) => {
          // Each card shows the variant the terminal would paint RIGHT NOW (the resolved mode).
          const t = resolveTermScheme(sch.id, theme);
          const on = scheme === sch.id;
          const low = lowContrast(t);
          return (
            <button
              key={sch.id}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={rovingTabIndex(on, i, true)}
              aria-describedby={low ? `termscheme-low-${sch.id}` : undefined}
              className={on ? `${a.scheme} ${a.on}` : a.scheme}
              onClick={() => setScheme(sch.id)}
            >
              <span
                className={a.schemeSample}
                style={{ background: t.background, color: t.foreground }}
                aria-hidden="true"
              >
                <span>
                  <span style={{ color: t.ansi.green }}>~/app</span>{" "}
                  <span style={{ color: t.ansi.blue }}>main</span> $ npm test
                </span>
                <span>
                  <span style={{ color: t.ansi.brightGreen }}>&#10003;</span> 214 passed{" "}
                  <span style={{ color: t.ansi.brightBlack }}>(3.1 s)</span>
                </span>
                <span>
                  <span style={{ color: t.ansi.red }}>&#10007;</span> 1 failed{" "}
                  <span style={{ color: t.ansi.yellow }}>warn</span>{" "}
                  <span style={{ color: t.ansi.magenta }}>x.ts</span>{" "}
                  <span style={{ color: t.ansi.cyan }}>#42</span>
                </span>
              </span>
              <span className={a.ansiStrip} aria-hidden="true">
                {Object.values(t.ansi).map((c, i) => (
                  <i key={i} style={{ background: c }} />
                ))}
              </span>
              <span className={a.schemeLabel}>
                <span className={a.radio} aria-hidden="true" />
                <span className={a.schemeName}>{sch.label}</span>
                <span className={a.tag}>{sch.light ? "Follows mode" : "Dark only"}</span>
              </span>
              {low && (
                <span className={a.schemeNote} id={`termscheme-low-${sch.id}`}>
                  Some colours low contrast
                </span>
              )}
            </button>
          );
        })}
      </div>

      <h3 className={styles.subhead} id="termfont-h">
        Terminal font
      </h3>
      <p className={styles.hint}>
        The face every agent renders in — every agent and the plain shell share one
        terminal, so this is one choice, not one per engine. Faces this device doesn&rsquo;t have are greyed
        out rather than quietly falling back. Saved per device, like the size below.
      </p>
      <div
        className={styles.fonts}
        role="radiogroup"
        aria-labelledby="termfont-h"
      >
        {TERM_FONT_PRESETS.map((f) => {
          const available = fontAvailability.get(f.id) ?? true;
          const active = !customOpen && termFontFamily === f.stack;
          return (
            <button
              key={f.id}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={!available}
              className={
                active
                  ? `${styles.fontCard} ${styles.active}`
                  : styles.fontCard
              }
              onClick={() => choosePreset(f.stack)}
            >
              <span className={styles.fontName} style={{ fontFamily: f.stack }}>
                {f.label}
              </span>
              {/* The specimen is deliberately the characters that separate one mono
                  face from another: zero vs capital O, one vs lowercase L vs capital
                  I. Rendered in the card's OWN face — that is the whole control. */}
              <span
                className={styles.fontSpecimen}
                style={{ fontFamily: f.stack }}
                aria-hidden="true"
              >
                0O1lI {"{}"} 8B5S
              </span>
              <span className={styles.fontNote}>
                {available ? f.note : "Not on this device"}
              </span>
            </button>
          );
        })}
        <button
          type="button"
          role="radio"
          aria-checked={customOpen}
          className={
            customOpen
              ? `${styles.fontCard} ${styles.custom} ${styles.active}`
              : `${styles.fontCard} ${styles.custom}`
          }
          onClick={() => {
            setCustomOpen(true);
            setFontDraft((d) => d || termFontFamily);
          }}
        >
          <span className={styles.fontName}>Custom&hellip;</span>
          <span
            className={styles.fontSpecimen}
            style={{ fontFamily: termFontFamily }}
            aria-hidden="true"
          >
            0O1lI {"{}"} 8B5S
          </span>
          <span className={styles.fontNote}>Type a CSS stack</span>
        </button>
      </div>
      {customOpen && (
        <>
          <div className={styles.fontCustomRow}>
            <input
              type="text"
              inputMode="text"
              spellCheck={false}
              autoCapitalize="none"
              autoCorrect="off"
              maxLength={TERM_FONT_FAMILY_MAX_LEN}
              className={
                fontDraftError
                  ? `${styles.fontCustomInput} ${styles.invalid}`
                  : styles.fontCustomInput
              }
              aria-label="Custom font stack"
              aria-invalid={fontDraftError ? true : undefined}
              aria-describedby={
                fontDraftError ? "termfont-err" : undefined
              }
              placeholder={DEFAULT_TERM_FONT_FAMILY}
              value={fontDraft}
              onChange={(e) => {
                setFontDraft(e.target.value);
                setFontDraftDirty(true);
                if (fontDraftError) setFontDraftError("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitFontDraft();
                }
              }}
              onBlur={commitFontDraft}
            />
          </div>
          {fontDraftError && (
            <p className={styles.fontCustomError} id="termfont-err" role="alert">
              {fontDraftError}
            </p>
          )}
        </>
      )}

      <h3 className={styles.subhead} id="termsize-h">
        Terminal text size
      </h3>
      <p className={styles.hint}>
        Sets how many columns the agent sees. Smaller text means a wider
        terminal, which is what a column-laid-out TUI like opencode
        needs — on a phone the shipped 13&nbsp;px leaves it only about
        50 columns. Saved per device, so a phone and a desktop can
        differ.
      </p>
      <div className={styles.termSize}>
        <div
          className={styles.termSizeStepper}
          role="group"
          aria-labelledby="termsize-h"
        >
          <button
            type="button"
            className={styles.termSizeStep}
            aria-label="Smaller terminal text"
            disabled={termFontSize <= TERM_FONT_SIZE_MIN}
            onClick={() =>
              setTermFontSize(stepTermFontSize(termFontSize, -1))
            }
          >
            &minus;
          </button>
          {/* aria-live so the value is announced on each step — the buttons keep focus,
              so without it a screen-reader user gets no feedback that anything changed. */}
          <output
            className={styles.termSizeValue}
            aria-live="polite"
          >{`${termFontSize} px`}</output>
          <button
            type="button"
            className={styles.termSizeStep}
            aria-label="Bigger terminal text"
            disabled={termFontSize >= TERM_FONT_SIZE_MAX}
            onClick={() =>
              setTermFontSize(stepTermFontSize(termFontSize, 1))
            }
          >
            +
          </button>
        </div>
        <button
          type="button"
          className={styles.termSizeReset}
          disabled={termFontSize === DEFAULT_TERM_FONT_SIZE}
          onClick={() => setTermFontSize(DEFAULT_TERM_FONT_SIZE)}
        >
          {`Reset to ${DEFAULT_TERM_FONT_SIZE} px`}
        </button>
        {/* A sample at the chosen size AND the chosen face, NOT a column count:
            Settings can be routed with no terminal mounted, so any number here would
            be a guess. The live count belongs to the in-session quick zoom, where
            term.cols is authoritative.

            It renders in `termFontFamily` — until #866 this used the CHROME's
            --font-mono stack, i.e. it previewed a face the terminal would never use.
            The box rules and the block bar are the payload: a face without
            U+2500/U+2580 coverage falls back per-glyph at a different advance width
            and the right-hand column visibly drifts, which is the one defect an
            operator must be able to see BEFORE living with the face all day. */}
        <p
          className={styles.termSizeSample}
          style={{
            fontSize: `${termFontSize}px`,
            fontFamily: termFontFamily,
          }}
        >
          {"┌─ opencode ───────────┬────────────┐\n"}
          {"│ build agent-sessions │ 0O1lI 8B5S │\n"}
          {"│ ✓ 214 passed         │ ▁▃▅▇█  62% │\n"}
          {"└──────────────────────┴────────────┘"}
        </p>
      </div>
    </section>
  );
}
