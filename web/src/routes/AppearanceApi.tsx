import { useState, type CSSProperties } from "react";
import chat from "../components/chat/ChatPane.module.css";
import { Markdown } from "../components/structured/Markdown";
import structured from "../components/structured/StructuredPane.module.css";
import { onRadioGroupKeyDown, rovingTabIndex } from "../lib/radioArrows";
import {
  API_BACKGROUND_IDS,
  API_BACKGROUND_LABELS,
  API_FONT_CUSTOM_MAX_LEN,
  API_PALETTE_IDS,
  API_PALETTE_LABELS,
  API_TEXT_SIZE_DEFAULT,
  API_TEXT_SIZE_MAX,
  API_TEXT_SIZE_MIN,
  apiPalette,
  DEFAULT_API_LOOK,
  isApiFontStack,
  isDefaultApiLook,
  type ApiBodyFont,
  type ApiCodeFont,
  type ApiLook,
} from "../theme/apiLook";
import { useApiLogProps, useApiLook } from "../theme/apiLookStore";
import { useTheme } from "../theme/themeStore";
import a from "./Appearance.module.css";
import styles from "./Settings.module.css";

// The preview's reply. It goes through the real <Markdown>, so every construct the pane styles
// shows here as it will there. The long unbroken line is deliberate: code must scroll inside its
// own block and never push the page sideways (#1494 P3, long-content state).
const SAMPLE_REPLY =
  "The **diff tokens** mix the status hue at 8% on light, too faint on `--surface-3`.\n\n" +
  "```css\n--diff-add-bg: color-mix(in srgb, var(--status-up) 14%, transparent);\n" +
  "const aVeryLongIdentifierThatNeverWrapsAndMustScrollInsideItsOwnBlock = resolveTheDiffTokensForTheLightGround();\n```";

const BODY_FONTS: { id: ApiBodyFont; label: string; sample?: string }[] = [
  { id: "sans", label: "System sans" },
  { id: "serif", label: "Serif", sample: '"Iowan Old Style", Charter, Georgia, "DejaVu Serif", serif' },
  { id: "mono", label: "Mono", sample: "var(--font-mono)" },
  { id: "custom", label: "Custom…" },
];
const CODE_FONTS: { id: ApiCodeFont; label: string }[] = [
  { id: "app", label: "App mono" },
  { id: "terminal", label: "Terminal font" },
  { id: "custom", label: "Custom…" },
];

/** A free-text font stack, committed on Enter/blur, refused with a reason (same rules as the
 *  terminal font: the stack lands in a CSS declaration). */
function StackField({
  label,
  initial,
  onCommit,
}: {
  label: string;
  initial: string;
  onCommit: (stack: string) => void;
}) {
  const [draft, setDraft] = useState(initial);
  const [error, setError] = useState("");
  const id = `${label.replace(/\W+/g, "-").toLowerCase()}-err`;
  const commit = () => {
    const s = draft.trim();
    if (!s) return;
    if (!isApiFontStack(s)) {
      setError(
        s.length > API_FONT_CUSTOM_MAX_LEN
          ? `Too long — ${API_FONT_CUSTOM_MAX_LEN} characters max.`
          : "Not a usable font stack. Check for an unclosed quote, an empty name between commas, or a character other than letters, digits, spaces, commas, hyphens, dots and quotes.",
      );
      return;
    }
    setError("");
    onCommit(s);
  };
  return (
    <>
      <input
        type="text"
        className={error ? `${a.stackInput} ${a.invalid}` : a.stackInput}
        aria-label={label}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? id : undefined}
        spellCheck={false}
        autoCapitalize="none"
        autoCorrect="off"
        maxLength={API_FONT_CUSTOM_MAX_LEN}
        placeholder='"Iowan Old Style", Georgia, serif'
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError("");
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          }
        }}
        onBlur={commit}
      />
      {error && (
        <p className={a.stackError} id={id} role="alert">
          {error}
        </p>
      )}
    </>
  );
}

/** Settings → Appearance → API sessions (#1494 P3): how the conversation in an API session reads.
 *  Every change shows in the live preview first, which renders through the pane's own CSS
 *  modules and the real <Markdown>, so it cannot drift from the pane. */
export function AppearanceApi() {
  const { look, setLook } = useApiLook();
  const { theme } = useTheme();
  const previewProps = useApiLogProps();
  // "Custom…" opens its field; the look only changes once a usable stack is committed, because a
  // custom font without a stack is not a look (coerceApiLook would drop it).
  const [bodyCustom, setBodyCustom] = useState(look.body_font === "custom");
  const [codeCustom, setCodeCustom] = useState(look.code_font === "custom");
  const set = (patch: Partial<ApiLook>) => setLook({ ...look, ...patch });

  const chips = (id: (typeof API_PALETTE_IDS)[number]): string[] => {
    const p = apiPalette(id)?.[theme];
    // BattleLab IS the app's tokens: its chips are the live ones.
    return p
      ? [p.bg, p.panel, p.text, p.you, p.rule]
      : ["var(--bg)", "var(--bg-1)", "var(--text)", "var(--accent)", "var(--text-muted)"];
  };
  const fontName = (BODY_FONTS.find((f) => f.id === look.body_font) ?? BODY_FONTS[0]).label;

  return (
    <section className={styles.section} aria-labelledby="api-appearance-h">
      <h2 id="api-appearance-h">API sessions</h2>
      <p className={styles.hint}>
        How the conversation in an API session reads: codex-api, claude-api, opencode-api and
        chat agents. Terminal sessions and the pane&rsquo;s own controls are not affected. Saved
        per device.
      </p>

      <div className={a.preview} data-testid="api-look-preview">
        <p className={a.previewHead}>
          <span>Live preview</span>
          <span>
            {API_PALETTE_LABELS[look.palette]} · {API_BACKGROUND_LABELS[look.background]} ·{" "}
            {look.body_font === "custom" ? "Custom" : fontName} · {look.text_size} px
          </span>
        </p>
        <div className={`${chat.log} ${a.previewLog}`} {...previewProps}>
          <div className={chat.exchange}>
            <div className={`${chat.turn} ${chat.user}`}>
              <div className={chat.who}>
                you <span className={chat.ts}>10:42</span>
              </div>
              <div className={chat.txt}>Why does the light-mode diff look washed out?</div>
            </div>
            <div className={`${chat.turn} ${chat.asst} ${structured.reply}`}>
              <div className={chat.who}>
                agent <span className={chat.ts}>10:42</span>
              </div>
              <div className={chat.txt}>
                <Markdown text={SAMPLE_REPLY} />
              </div>
            </div>
          </div>
        </div>
      </div>

      <h3 className={styles.subhead} id="api-palette-h">
        Palette
      </h3>
      <p className={styles.hint}>
        Ground, panel, text, your messages and the agent&rsquo;s rule. Each palette has a dark and a
        light variant and follows Interface &rarr; Mode.
      </p>
      <div
        className={a.palettes}
        role="radiogroup"
        aria-labelledby="api-palette-h"
        onKeyDown={onRadioGroupKeyDown}
      >
        {API_PALETTE_IDS.map((id, i) => {
          const on = look.palette === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={rovingTabIndex(on, i, true)}
              className={on ? `${a.palette} ${a.on}` : a.palette}
              onClick={() => set({ palette: id })}
            >
              <span className={a.chips} aria-hidden="true">
                {chips(id).map((c, i) => (
                  <i key={i} style={{ background: c }} />
                ))}
              </span>
              <span className={a.schemeLabel}>
                <span className={a.radio} aria-hidden="true" />
                <span className={a.schemeName}>{API_PALETTE_LABELS[id]}</span>
                <span className={a.tag}>{id === "battlelab" ? "Default" : "Follows mode"}</span>
              </span>
            </button>
          );
        })}
      </div>

      <h3 className={styles.subhead} id="api-bg-h">
        Background
      </h3>
      <div
        className={a.backgrounds}
        role="radiogroup"
        aria-labelledby="api-bg-h"
        onKeyDown={onRadioGroupKeyDown}
      >
        {API_BACKGROUND_IDS.map((id, i) => {
          const on = look.background === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={on}
              tabIndex={rovingTabIndex(on, i, true)}
              className={on ? `${a.bgCard} ${a.on}` : a.bgCard}
              onClick={() => set({ background: id })}
            >
              <span
                className={`${chat.log} ${a.bgTile}`}
                data-api-bg={id === "none" ? undefined : id}
                aria-hidden="true"
              />
              <span className={a.schemeLabel}>
                <span className={a.radio} aria-hidden="true" />
                <span className={a.schemeName}>{API_BACKGROUND_LABELS[id]}</span>
              </span>
            </button>
          );
        })}
      </div>

      <div className={a.fontRows}>
        <div>
          <h3 className={styles.subhead} id="api-body-h">
            Text font
          </h3>
          <div
            className={a.segs}
            role="radiogroup"
            aria-labelledby="api-body-h"
            onKeyDown={onRadioGroupKeyDown}
          >
            {BODY_FONTS.map((f, i) => {
              const on = f.id === "custom" ? bodyCustom : !bodyCustom && look.body_font === f.id;
              return (
                <button
                  key={f.id}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={rovingTabIndex(on, i, true)}
                  className={on ? `${a.seg} ${a.on}` : a.seg}
                  style={f.sample ? ({ fontFamily: f.sample } as CSSProperties) : undefined}
                  onClick={() => {
                    if (f.id === "custom") return setBodyCustom(true);
                    setBodyCustom(false);
                    set({ body_font: f.id, body_font_custom: undefined });
                  }}
                >
                  {f.label}
                </button>
              );
            })}
          </div>
          {bodyCustom && (
            <StackField
              label="Custom text font stack"
              initial={look.body_font_custom ?? ""}
              onCommit={(s) => set({ body_font: "custom", body_font_custom: s })}
            />
          )}
        </div>
        <div>
          <h3 className={styles.subhead} id="api-code-h">
            Code font
          </h3>
          <div
            className={a.segs}
            role="radiogroup"
            aria-labelledby="api-code-h"
            onKeyDown={onRadioGroupKeyDown}
          >
            {CODE_FONTS.map((f, i) => {
              const on = f.id === "custom" ? codeCustom : !codeCustom && look.code_font === f.id;
              return (
                <button
                  key={f.id}
                  type="button"
                  role="radio"
                  aria-checked={on}
                  tabIndex={rovingTabIndex(on, i, true)}
                  className={on ? `${a.seg} ${a.on}` : a.seg}
                  onClick={() => {
                    if (f.id === "custom") return setCodeCustom(true);
                    setCodeCustom(false);
                    set({ code_font: f.id, code_font_custom: undefined });
                  }}
                >
                  {f.label}
                </button>
              );
            })}
          </div>
          {codeCustom && (
            <StackField
              label="Custom code font stack"
              initial={look.code_font_custom ?? ""}
              onCommit={(s) => set({ code_font: "custom", code_font_custom: s })}
            />
          )}
        </div>
      </div>

      <h3 className={styles.subhead} id="api-size-h">
        Text size
      </h3>
      <div className={styles.termSize}>
        <div className={styles.termSizeStepper} role="group" aria-labelledby="api-size-h">
          <button
            type="button"
            className={styles.termSizeStep}
            aria-label="Smaller conversation text"
            disabled={look.text_size <= API_TEXT_SIZE_MIN}
            onClick={() => set({ text_size: look.text_size - 1 })}
          >
            &minus;
          </button>
          <output className={styles.termSizeValue} aria-live="polite">{`${look.text_size} px`}</output>
          <button
            type="button"
            className={styles.termSizeStep}
            aria-label="Bigger conversation text"
            disabled={look.text_size >= API_TEXT_SIZE_MAX}
            onClick={() => set({ text_size: look.text_size + 1 })}
          >
            +
          </button>
        </div>
        <button
          type="button"
          className={styles.termSizeReset}
          disabled={look.text_size === API_TEXT_SIZE_DEFAULT}
          onClick={() => set({ text_size: API_TEXT_SIZE_DEFAULT })}
        >
          {`Reset to ${API_TEXT_SIZE_DEFAULT} px`}
        </button>
      </div>

      <div className={a.footRow}>
        <span>The defaults are today&rsquo;s look, exactly.</span>
        <button
          type="button"
          className={a.reset}
          disabled={isDefaultApiLook(look)}
          onClick={() => {
            setBodyCustom(false);
            setCodeCustom(false);
            setLook({ ...DEFAULT_API_LOOK });
          }}
        >
          Reset API sessions
        </button>
      </div>
    </section>
  );
}
