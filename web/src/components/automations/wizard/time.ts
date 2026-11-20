/** The wizard's time helpers (#2221 P2b): the zones the browser knows, and a unix time as a
 *  `datetime-local` value in the viewer's zone. */

/** Every IANA zone the browser knows, when it can say; otherwise the field is free text. */
export function timeZones(): string[] | null {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  try {
    return intl.supportedValuesOf ? intl.supportedValuesOf("timeZone") : null;
  } catch {
    return null;
  }
}

/** A unix time as a `datetime-local` value in the viewer's zone, and back. */
export function toLocalInput(ts: number | null): string {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function fromLocalInput(v: string): number | null {
  if (!v) return null;
  const t = new Date(v).getTime();
  return Number.isFinite(t) ? t / 1000 : null;
}
