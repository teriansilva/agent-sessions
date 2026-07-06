// Extract image files from a clipboard/drag DataTransfer so they can be uploaded via
// /api/upload (#135). Terminals are text-only, so a pasted screenshot is otherwise
// dropped; we lift the image bytes out here and the caller turns the upload's server
// path into something the agent can read.

/** A materialized File counts as an image when it says so — or when the clipboard backend
 *  dropped the MIME type but the filename still carries an image extension (#530). */
function isImageFile(f: File): boolean {
  return (
    f.type.startsWith("image/") || (!f.type && /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(f.name))
  );
}

/** The image `File`s carried by a DataTransfer (clipboard or drop), or `[]` if none.
 *  Reads `items` first (the clipboard shape) and falls back to `files` (some browsers /
 *  drops populate only that). Non-image entries are ignored — plain-text paste is left
 *  for the terminal/textarea to handle normally. */
export function imageFilesFromData(dt: DataTransfer | null | undefined): File[] {
  if (!dt) return [];
  const out: File[] = [];
  const seen = new Set<string>();
  const add = (f: File | null) => {
    if (!f || !isImageFile(f)) return;
    const sig = `${f.name}:${f.size}:${f.type}`;
    if (seen.has(sig)) return;
    seen.add(sig);
    out.push(f);
  };
  // Materialize EVERY file-kind item and filter on the materialized File, never on the
  // enumeration-time `item.type`: deferred clipboard backends (Chrome 149's lazy format
  // reads) can leave `DataTransferItem.type` empty until the item is materialized, so a
  // type pre-filter here silently drops real images (#530).
  for (const item of Array.from(dt.items ?? [])) {
    if (item.kind === "file") add(item.getAsFile());
  }
  for (const f of Array.from(dt.files ?? [])) add(f);
  return out;
}

/** Image files read straight off the async clipboard (`navigator.clipboard.read()`) —
 *  the fallback for paste events whose DataTransfer yields no usable file even though
 *  the OS clipboard holds an image (#530, observed on Windows Chrome 149). Fail-soft:
 *  an unsupported API, denied permission, or unfocused document all yield `[]`. */
export async function imageFilesFromAsyncClipboard(): Promise<File[]> {
  try {
    const items = await navigator.clipboard.read();
    const out: File[] = [];
    for (const item of items) {
      const type = item.types.find((t) => t.startsWith("image/"));
      if (!type) continue;
      const blob = await item.getType(type);
      const ext = (type.split("/")[1] ?? "png").split("+")[0];
      out.push(new File([blob], `clipboard.${ext}`, { type }));
    }
    return out;
  } catch {
    return [];
  }
}
