/** The AI endpoint's provider rules on the client (#1657, #1671). The SERVER decides — every save,
 *  test and send is bound there to the lock-current provider and origin — so everything here is
 *  feedback: what the warning says, and which controls wait for the acknowledgement. */
import type { AiEgressAck, AiProvider } from "../types/api";

export const CUSTOM_ID = "custom";

/** What the picker shows when the catalog cannot be loaded: Custom alone — today's form. */
export const CUSTOM_ROW: AiProvider = {
  id: CUSTOM_ID,
  label: "Custom (OpenAI-compatible)",
  group: "custom",
  kind: "openai-chat",
  base_url: "",
  url_editable: true,
  vendor: "",
  thinking_supported: true,
  auto_model: "",
  key_hint: "",
};

export const isHosted = (row: AiProvider) => row.group === "api";

/** The host AS TYPED, lower-cased, brackets dropped — never the browser's normalized form. The
 *  server reads the raw text (`urlsplit`), and `new URL()` would turn `127.1` into `127.0.0.1`,
 *  an IDN into punycode and an IPv6 literal into its short form: the page would then promise
 *  "loopback" or "covered" where the server says otherwise (second review on #1672). */
function rawHost(url: string): { scheme: string; host: string; port: string } | null {
  const m = /^\s*([a-z][a-z0-9+.-]*):\/\/(?:[^@/?#]*@)?(\[[^\]]*\]|[^/:?#]*)(?::(\d*))?/i.exec(url);
  if (!m || !m[2]) return null;
  return {
    scheme: m[1].toLowerCase(),
    host: m[2].toLowerCase().replace(/^\[|\]$/g, ""),
    port: m[3] ?? "",
  };
}

const LOOPBACK_V4 = /^127\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

/** The trust boundary is THIS host: a literal loopback IP or exactly `localhost` — the same
 *  rule as `ai_providers.is_loopback`, on the same raw text. A LAN address, a `.local` name or a
 *  non-canonical spelling such as `127.1` is not exempt (the server would refuse it unacked). */
export function isLoopback(url: string): boolean {
  const r = rawHost(url);
  if (r === null) return false;
  if (r.host === "localhost" || r.host === "::1" || r.host === "0:0:0:0:0:0:0:1") return true;
  return LOOPBACK_V4.test(r.host);
}

/** Whether sending to this URL needs the operator's acknowledgement. */
export const needsConsent = (url: string) =>
  url.trim() !== "" && !isLoopback(url);

/** The server's origin form (`ai_providers._origin`), from the raw text: scheme, lower-cased
 *  host as typed (IPv6 without brackets), explicit port or the scheme's default. */
function serverOrigin(url: string): string | null {
  const r = rawHost(url);
  if (r === null || (r.scheme !== "http" && r.scheme !== "https")) return null;
  return `${r.scheme}://${r.host}:${r.port || (r.scheme === "https" ? "443" : "80")}`;
}

/** Whether a stored acknowledgement already covers this provider + URL. */
export function ackCovers(
  ack: AiEgressAck | null | undefined,
  provider: string,
  url: string,
): boolean {
  const origin = serverOrigin(url);
  return (
    !!ack &&
    origin !== null &&
    ack.provider === provider &&
    ack.origin === origin
  );
}

/** Who the warning names: a hosted vendor, or the host of a Custom / self-hosted URL. */
export function recipient(row: AiProvider, url: string): string {
  if (isHosted(row)) return row.vendor || row.label;
  return rawHost(url)?.host || url.trim() || row.label;
}

/** The catalog grouped for the picker, in the table's order. Subscriptions arrive with P2. */
export function groups(
  rows: AiProvider[],
): { title: string; rows: AiProvider[] }[] {
  const selfHosted = rows.filter(
    (r) => r.group === "custom" || r.group === "self-hosted",
  );
  const hosted = rows.filter((r) => r.group === "api");
  return [
    { title: "Self-hosted · your server", rows: selfHosted },
    { title: "API · your key", rows: hosted },
  ].filter((g) => g.rows.length > 0);
}
