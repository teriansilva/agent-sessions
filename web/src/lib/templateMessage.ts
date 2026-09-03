/** Message assembly and `{{field}}` substitution for instruction templates (#905).
 *
 *  One source of truth for "what the agent receives". `assembleMessage` is the exact rule
 *  `Compose.send()` has always used — trimmed text first, then each attachment path, joined by
 *  single spaces, raw and unquoted — lifted out so the template editor's preview and the
 *  composer's send cannot drift (the P2 contract on #905). If this function changes, the
 *  preview and the paste change together.
 *
 *  Substitution is a literal replace of `{{name}}` for DECLARED fields only. There is no
 *  expression language, no escaping, and a token that names no field is left in the text
 *  verbatim (the editor flags it; the picker never invents a field). */

export interface TemplateFieldLike {
  name: string;
  label?: string;
  default?: string;
  required?: boolean;
}

/** The server's field-name shape (`templates.FIELD_NAME_RE`), global so it can be iterated. */
export const FIELD_TOKEN_RE = /\{\{([a-z][a-z0-9_]{0,31})\}\}/g;

/** `Compose.send()`'s message: trimmed text (if any) then every attachment path, space-joined. */
export function assembleMessage(text: string, attachmentPaths: readonly string[]): string {
  const parts: string[] = [];
  if (text.trim()) parts.push(text.trim());
  for (const p of attachmentPaths) parts.push(p);
  return parts.join(" ");
}

/** Every distinct `{{token}}` in `body`, in first-appearance order. */
export function tokensIn(body: string): string[] {
  const out: string[] = [];
  for (const m of body.matchAll(FIELD_TOKEN_RE)) {
    if (!out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

/** Tokens in `body` that no declared field covers — sent literally, so worth a warning. */
export function unknownTokens(body: string, fields: readonly TemplateFieldLike[]): string[] {
  const declared = new Set(fields.map((f) => f.name));
  return tokensIn(body).filter((t) => !declared.has(t));
}

/** Replace `{{name}}` for every declared field that has a value. A declared field with no
 *  value (`undefined`) keeps its token, so a preview shows the slot rather than a blank. */
export function substituteFields(
  body: string,
  fields: readonly TemplateFieldLike[],
  values: Readonly<Record<string, string | undefined>>,
): string {
  const declared = new Map(fields.map((f) => [f.name, f]));
  return body.replace(FIELD_TOKEN_RE, (token, name: string) => {
    if (!declared.has(name)) return token;
    const v = values[name];
    return v === undefined ? token : v;
  });
}

/** The values a preview substitutes: each field's non-empty default. */
export function defaultValues(fields: readonly TemplateFieldLike[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of fields) if (f.default) out[f.name] = f.default;
  return out;
}

/** Required fields whose effective value (given, else default) is blank. */
export function missingRequired(
  fields: readonly TemplateFieldLike[],
  values: Readonly<Record<string, string | undefined>>,
): string[] {
  return fields
    .filter((f) => f.required && !(values[f.name] ?? f.default ?? "").trim())
    .map((f) => f.name);
}

/** The full message a template send would paste: substituted body + image paths. */
export function renderTemplate(
  t: { body: string; fields: readonly TemplateFieldLike[]; images: readonly { path: string }[] },
  values: Readonly<Record<string, string | undefined>>,
): string {
  return assembleMessage(
    substituteFields(t.body, t.fields, values),
    t.images.map((i) => i.path),
  );
}

/** The stored basename of an upload path — the key `GET /api/uploads/{stored}` reads back. */
export function uploadStoredName(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? path : path.slice(i + 1);
}
