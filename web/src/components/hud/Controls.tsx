/** NIGHTJAR controls (#1531, docs/design.md v2 §6): Chip, Seg (radio group), Tabs, Switch and
 *  Disclosure — the ONE implementation of each, so their motion (§8: a sliding marker, a sliding
 *  knob, grid-rows expansion) has one definition. All hold the 44 px floor under a coarse pointer
 *  or at ≤800px, and focus is the inset `--accent-text` ring. */
import {
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useCallback,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

export function Chip({
  on = false,
  onClick,
  children,
  count,
  className = "",
  title,
}: {
  on?: boolean;
  onClick?: () => void;
  children: ReactNode;
  count?: ReactNode;
  className?: string;
  title?: string;
}) {
  const body = (
    <>
      {children}
      {count !== undefined && <span className="nj-n">{count}</span>}
    </>
  );
  if (!onClick)
    return (
      <span className={`nj-chip${on ? " is-on" : ""} ${className}`.trim()} title={title}>
        {body}
      </span>
    );
  return (
    <button
      type="button"
      className={`nj-chip ${className}`.trim()}
      aria-pressed={on}
      onClick={onClick}
      title={title}
    >
      {body}
    </button>
  );
}

/** The marker box under the selected item, measured from the item itself (transform only). */
function useMarker(selectedIndex: number, deps: unknown[]) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState<{ x: number; y: number; w: number; h: number } | null>(null);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host) return;
    const measure = () => {
      const items = host.querySelectorAll<HTMLElement>("[data-nj-item]");
      const el = items[selectedIndex];
      setBox(
        el ? { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight } : null,
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(host);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIndex, ...deps]);
  return { ref, box };
}

export interface SegOption<T extends string> {
  value: T;
  label: ReactNode;
  disabled?: boolean;
}

/** A segmented radio group: one Tab stop, arrow keys move and select (WAI-ARIA radio group). */
export function Seg<T extends string>({
  options,
  value,
  onChange,
  label,
  className = "",
  testId,
}: {
  options: readonly SegOption<T>[];
  value: T;
  onChange: (v: T) => void;
  label: string;
  className?: string;
  testId?: string;
}) {
  const idx = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  const { ref, box } = useMarker(idx, [options.length]);
  const move = (e: KeyboardEvent<HTMLDivElement>) => {
    const delta = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!delta) return;
    e.preventDefault();
    const enabled = options.filter((o) => !o.disabled);
    const at = enabled.findIndex((o) => o.value === value);
    const next = enabled[(at + delta + enabled.length) % enabled.length];
    if (!next) return;
    onChange(next.value);
    const btn = ref.current?.querySelector<HTMLElement>(`[data-value="${CSS.escape(next.value)}"]`);
    btn?.focus();
  };
  return (
    <div
      ref={ref}
      className={`nj-seg ${className}`.trim()}
      role="radiogroup"
      aria-label={label}
      onKeyDown={move}
      data-testid={testId}
    >
      {box && (
        <span
          className="nj-seg-marker"
          aria-hidden="true"
          style={
            {
              "--x": `${box.x}px`,
              "--y": `${box.y}px`,
              "--w": `${box.w}px`,
              "--h": `${box.h}px`,
            } as CSSProperties
          }
        />
      )}
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            disabled={o.disabled}
            data-nj-item=""
            data-value={o.value}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export interface TabDef<T extends string> {
  id: T;
  label: ReactNode;
}

/** Tabs with a sliding marker; the chosen panel cross-fades in (`.nj-tabpanel`). Arrow keys move
 *  focus and select (automatic activation); Home / End jump. */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
  panel,
  className = "",
}: {
  tabs: readonly TabDef<T>[];
  value: T;
  onChange: (id: T) => void;
  label: string;
  /** Renders the selected tab's panel. */
  panel?: (id: T) => ReactNode;
  className?: string;
}) {
  const base = useId();
  const idx = Math.max(
    0,
    tabs.findIndex((t) => t.id === value),
  );
  const { ref, box } = useMarker(idx, [tabs.length]);
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    let next = -1;
    if (e.key === "ArrowRight") next = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(tabs[next].id);
    ref.current?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
  };
  return (
    <>
      <div ref={ref} className={`nj-tabs ${className}`.trim()} role="tablist" aria-label={label} onKeyDown={onKey}>
        {box && (
          <span
            className="nj-tab-marker"
            aria-hidden="true"
            style={{ "--x": `${box.x}px`, "--w": `${box.w}px` } as CSSProperties}
          />
        )}
        {tabs.map((t) => {
          const on = t.id === value;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`${base}-tab-${t.id}`}
              aria-selected={on}
              aria-controls={panel ? `${base}-panel-${t.id}` : undefined}
              tabIndex={on ? 0 : -1}
              data-nj-item=""
              onClick={() => onChange(t.id)}
            >
              {t.label}
            </button>
          );
        })}
      </div>
      {panel && (
        <div
          key={value}
          className="nj-tabpanel"
          role="tabpanel"
          id={`${base}-panel-${value}`}
          aria-labelledby={`${base}-tab-${value}`}
        >
          {panel(value)}
        </div>
      )}
    </>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
  testId,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** The accessible name; pair it with a visible label beside the switch. */
  label: string;
  disabled?: boolean;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      className="nj-switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      data-testid={testId}
    />
  );
}

/** An expanding section: grid-rows 0fr → 1fr on ONE container plus a content fade. The body stays
 *  mounted while closed (editors inside keep their state) and is `inert` so nothing hidden takes
 *  focus. */
export function Disclosure({
  summary,
  children,
  defaultOpen = false,
  open: controlled,
  onOpenChange,
  className = "",
  testId,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  testId?: string;
}) {
  const [own, setOwn] = useState(defaultOpen);
  const open = controlled ?? own;
  const id = useId();
  const toggle = useCallback(() => {
    const next = !open;
    if (controlled === undefined) setOwn(next);
    onOpenChange?.(next);
  }, [open, controlled, onOpenChange]);
  return (
    <div className={`nj-disclosure ${className}`.trim()} data-testid={testId}>
      <button
        type="button"
        className="nj-disclosure-toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
      >
        <span className="nj-chev" aria-hidden="true">
          ▸
        </span>
        {summary}
      </button>
      <div id={id} className={`nj-disclosure-body${open ? " is-open" : ""}`} inert={!open}>
        <div className="nj-disclosure-inner">{children}</div>
      </div>
    </div>
  );
}

interface FieldCommon {
  label: ReactNode;
  /** A short help line under the control (it describes the control: `aria-describedby`). */
  hint?: ReactNode;
  /** The failure, if any: marks the control `aria-invalid` and is announced (`role="alert"`). */
  error?: ReactNode;
  /** Mono text (paths, keys, code). */
  mono?: boolean;
  className?: string;
}
type FieldProps = FieldCommon &
  (
    | ({ as?: "input" } & Omit<InputHTMLAttributes<HTMLInputElement>, "className">)
    | ({ as: "select" } & Omit<SelectHTMLAttributes<HTMLSelectElement>, "className">)
    | ({ as: "textarea" } & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "className">)
  );

/** A labelled form control (docs/design.md v2 §6 "Form fields"): an input, select or textarea on
 *  the `nj-field` plate (chamfered, `--bg-2`, the edge lights on hover / focus, the inset focus
 *  ring, 44 px under a coarse pointer), its label above, then its hint and its error — both tied
 *  to the control with `aria-describedby`, the error also with `aria-invalid`. */
export function Field(props: FieldProps) {
  const { label, hint, error, mono = false, className = "", ...rest } = props;
  const auto = useId();
  const id = rest.id ?? `nj-field-${auto}`;
  const hintId = hint !== undefined ? `${id}-hint` : undefined;
  const errId = error !== undefined && error !== null && error !== false ? `${id}-error` : undefined;
  const describedBy = [hintId, errId].filter(Boolean).join(" ") || undefined;
  const cls = `nj-field${mono ? " is-mono" : ""}`;
  const shared = {
    id,
    className: cls,
    "aria-describedby": describedBy,
    "aria-invalid": errId ? (true as const) : undefined,
  };
  let control: ReactNode;
  if (rest.as === "select") {
    const { as: _as, ...p } = rest;
    void _as;
    control = <select {...p} {...shared} />;
  } else if (rest.as === "textarea") {
    const { as: _as, ...p } = rest;
    void _as;
    control = <textarea {...p} {...shared} />;
  } else {
    const { as: _as, ...p } = rest;
    void _as;
    control = <input {...p} {...shared} />;
  }
  return (
    <div className={`nj-fieldset ${className}`.trim()}>
      <label className="nj-label" htmlFor={id}>
        {label}
      </label>
      {control}
      {hintId && (
        <p className="nj-hint" id={hintId}>
          {hint}
        </p>
      )}
      {errId && (
        <p className="nj-error" id={errId} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
