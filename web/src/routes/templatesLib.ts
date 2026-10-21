import { ApiError } from "../lib/api";
import { relTime } from "../lib/format";
import type { Template, TemplateInput, TemplateLimits } from "../types/api";

/** Non-component helpers for the TEMPLATES gallery + editor (#905), kept out of the route
 *  files so fast refresh keeps working there (the `react-refresh/only-export-components` rule). */

export function errMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.message ? e.message : fallback;
}

/** The card's mono meta line: `2 fields · 2 images · used 14× · 2h ago`. Zero parts are
 *  omitted; "never used" is stated rather than shown as 0. */
export function metaLine(t: Template): string {
  const parts: string[] = [];
  if (t.fields.length) {
    parts.push(`${t.fields.length} ${t.fields.length === 1 ? "field" : "fields"}`);
  }
  if (t.images.length) {
    parts.push(`${t.images.length} ${t.images.length === 1 ? "image" : "images"}`);
  }
  parts.push(t.used_count ? `used ${t.used_count}×` : "never used");
  if (t.last_used_at) parts.push(relTime(t.last_used_at));
  return parts.join(" · ");
}

export const FIELD_NAME_RE = /^[a-z][a-z0-9_]{0,31}$/;
export const TAG_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;

/** Code points, not UTF-16 units: the server caps in Python characters. */
export const cp = (s: string) => [...s].length;

/** Client-side mirror of the server's rules — a courtesy that keeps SAVE honest, not the gate.
 *  The server re-checks everything and its 422 `detail` is what the error line shows. */
export function formProblems(f: TemplateInput, limits: TemplateLimits): string[] {
  const out: string[] = [];
  if (!f.name.trim()) out.push("name is required");
  if (cp(f.name) > limits.name_max) out.push(`name is too long (max ${limits.name_max})`);
  if (cp(f.description) > limits.description_max) {
    out.push(`description is too long (max ${limits.description_max})`);
  }
  if (!f.body.trim()) out.push("instructions are required");
  if (cp(f.body) > limits.body_max) {
    out.push(`instructions are too long (max ${limits.body_max})`);
  }
  if (f.tags.length > limits.tags_max) out.push(`too many tags (max ${limits.tags_max})`);
  if (f.fields.length > limits.fields_max) {
    out.push(`too many fields (max ${limits.fields_max})`);
  }
  const seen = new Set<string>();
  for (const fl of f.fields) {
    if (!FIELD_NAME_RE.test(fl.name)) out.push("a field name is lowercase, letters/digits/_");
    else if (seen.has(fl.name)) out.push(`duplicate field: ${fl.name}`);
    seen.add(fl.name);
    if (cp(fl.label) > limits.label_max) out.push(`label too long (max ${limits.label_max})`);
    if (cp(fl.default) > limits.default_max) {
      out.push(`default too long (max ${limits.default_max})`);
    }
    if (fl.kind === "secret" && fl.default) out.push(`${fl.name}: a secret field has no default`);
  }
  if (f.images.length > limits.images_max) {
    out.push(`too many images (max ${limits.images_max})`);
  }
  return out;
}
