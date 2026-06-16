// Extract image files from a clipboard/drag DataTransfer so they can be uploaded via
// /api/upload (#135). Terminals are text-only, so a pasted screenshot is otherwise
// dropped; we lift the image bytes out here and the caller turns the upload's server
// path into something the agent can read.

/** The image `File`s carried by a DataTransfer (clipboard or drop), or `[]` if none.
 *  Reads `items` first (the clipboard shape) and falls back to `files` (some browsers /
 *  drops populate only that). Non-image entries are ignored — plain-text paste is left
 *  for the terminal/textarea to handle normally. */
export function imageFilesFromData(dt: DataTransfer | null | undefined): File[] {
  if (!dt) return [];
  const out: File[] = [];
  const seen = new Set<string>();
  const add = (f: File | null) => {
    if (!f || !f.type.startsWith("image/")) return;
    const sig = `${f.name}:${f.size}:${f.type}`;
    if (seen.has(sig)) return;
    seen.add(sig);
    out.push(f);
  };
  for (const item of Array.from(dt.items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) add(item.getAsFile());
  }
  for (const f of Array.from(dt.files ?? [])) add(f);
  return out;
}
