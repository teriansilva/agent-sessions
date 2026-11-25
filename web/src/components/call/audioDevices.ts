/** The call's audio devices (#1670): what the browser reports, and which one a call uses.
 *
 *  A choice is a CURRENTLY LISTED device id. What the operator chose last is remembered in this
 *  browser (id + label, never sent anywhere). On the next call the remembered id is used if it is
 *  listed; else its label, but only when exactly one listed device has it; else the system default,
 *  with the reason said — audio is never silently routed to a same-named device. Pure: the
 *  browser's device APIs are passed in, so the rules are unit-tested. */

export type DeviceKind = "input" | "output";

export interface DeviceOption {
  id: string;
  label: string;
}

export interface DeviceLists {
  inputs: DeviceOption[];
  outputs: DeviceOption[];
}

export interface Remembered {
  id: string;
  label: string;
}

/** The browser's own device names, or a plain one where it gives none (no permission yet). */
export async function listDevices(md: Pick<MediaDevices, "enumerateDevices">): Promise<DeviceLists> {
  const all = await md.enumerateDevices();
  const pick = (kind: MediaDeviceKind, word: string): DeviceOption[] =>
    all
      .filter((d) => d.kind === kind && d.deviceId)
      .map((d, i) => ({ id: d.deviceId, label: d.label || `${word} ${i + 1}` }));
  return { inputs: pick("audioinput", "Microphone"), outputs: pick("audiooutput", "Speaker") };
}

const KEY = "battlelab.call.devices";

export function remembered(kind: DeviceKind): Remembered | null {
  try {
    const all = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, unknown>;
    const r = all[kind] as Partial<Remembered> | undefined;
    return r && typeof r.id === "string" && typeof r.label === "string" ? { id: r.id, label: r.label } : null;
  } catch {
    return null;
  }
}

export function remember(kind: DeviceKind, opt: DeviceOption | null): void {
  try {
    const all = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, unknown>;
    if (opt) all[kind] = { id: opt.id, label: opt.label };
    else delete all[kind];
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* storage unavailable: the choice holds for this call only */
  }
}

const WORD: Record<DeviceKind, string> = { input: "microphone", output: "speaker" };

/** Which listed device to use for a remembered choice: its id, else its UNIQUE label, else the
 *  default (`""`) with the reason to show. No remembered choice = the default, nothing to say. */
export function choose(
  kind: DeviceKind,
  list: DeviceOption[],
  want: Remembered | null,
): { id: string; reason: string } {
  if (!want) return { id: "", reason: "" };
  if (list.some((d) => d.id === want.id)) return { id: want.id, reason: "" };
  const named = list.filter((d) => d.label === want.label);
  if (named.length === 1) return { id: named[0].id, reason: "" };
  return {
    id: "",
    reason:
      named.length > 1
        ? `More than one device is called ${want.label} — using the default ${WORD[kind]}.`
        : `${want.label} isn't connected — using the default ${WORD[kind]}.`,
  };
}

/** What the sheet says when the device in use disappears mid-call. */
export function goneNotice(kind: DeviceKind, label: string): string {
  return `${label || `The ${WORD[kind]}`} was disconnected — now using the default ${WORD[kind]}.`;
}
