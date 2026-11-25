/** The boot sequence (#1531, docs/design.md v2 §8 rule 5): about 1.2 s, ONCE per visit (per tab,
 *  `sessionStorage`), Full motion only. It never blocks: the layer is click-through and the app is
 *  live underneath; any key, press or wheel dismisses it at once. It shows REAL reads only — the
 *  host the config named, the operator, the version — never invented telemetry. A read that has
 *  not arrived yet (a slow `/api/config`) has no line: no placeholder, no "—"; it appears if it
 *  lands while the boot still runs, and the boot never waits for it. While it runs the shell
 *  carries `booting` (App.css), so its panels power on in sequence. */
import s from "./BootSequence.module.css";

export function BootSequence({
  on,
  host,
  operator,
  version,
}: {
  on: boolean;
  host: string | null;
  operator: string | null;
  version: string | null;
}) {
  if (!on) return null;
  return (
    <div className={s.boot} aria-hidden="true" data-testid="boot-sequence">
      <div className={s.log}>
        <div className={s.head}>
          <span className="nj-notch" />
          Uplink // establishing
        </div>
        {host && (
          <div className={s.line}>
            <span className={s.ok}>✓</span> Host // <span className={s.val}>{host}</span>
          </div>
        )}
        {operator && (
          <div className={s.line}>
            <span className={s.ok}>✓</span> Operator // <span className={s.val}>{operator}</span>
          </div>
        )}
        {version && (
          <div className={s.line}>
            <span className={s.ok}>✓</span> BattleLab // <span className={s.val}>v{version}</span>
          </div>
        )}
        <div className={s.bar} />
      </div>
    </div>
  );
}
