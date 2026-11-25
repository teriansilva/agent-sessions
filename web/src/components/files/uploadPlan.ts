/** Planning an upload before a byte moves (#807).
 *
 *  Pure, DOM-free, and in its own module for the reason `gitModes.ts` is: it is the part worth
 *  unit-testing, and keeping it out of the components keeps fast refresh working.
 *
 *  **None of this is enforcement.** Every bound here is also enforced server-side, per chunk, in
 *  `filewrite.Batch.take_bytes`. These functions exist so an over-budget drop fails *fast and
 *  legibly* — with the limit named — instead of streaming for a minute and then dying.
 */

/** Kept in step with `filewrite.py`. A mismatch shows up as a server refusal the UI did not
 *  predict, which is safe but rude — so the numbers are stated on each side rather than guessed. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
export const MAX_BATCH_FILES = 500;
export const MAX_BATCH_BYTES = 250 * 1024 * 1024;
export const MAX_DEPTH = 32;

export interface PlannedFile {
  file: File;
  /** Path within the target directory. `notes.md` for a flat file, `src/lib/util.ts` in a folder. */
  relpath: string;
  size: number;
}

export interface UploadPlan {
  files: PlannedFile[];
  /** Files excluded before sending, each with the reason to show on its own row. */
  rejected: { relpath: string; reason: string }[];
  bytes: number;
  /** A whole-batch refusal — nothing is sent when this is set. */
  refusal: string | null;
}

const mb = (n: number) => Math.round(n / (1024 * 1024));

/** Human size for a queue row. Deliberately coarse: this is a progress strip, not an audit. */
export function humanSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${mb(n)} MB`;
}

/** True if the string carries a C0/DEL control character.
 *
 *  A codepoint scan rather than a regex literal: the literal form needs raw control bytes in the
 *  source, which is exactly the kind of thing that survives a copy-paste as an invisible defect.
 */
function hasControlChar(s: string): boolean {
  for (const ch of s) {
    const c = ch.codePointAt(0) ?? 0;
    if (c < 0x20 || c === 0x7f) return true;
  }
  return false;
}

/** Reject a relpath the server would reject, with the same reasoning, before sending it.
 *
 *  Returns the reason, or `null` when it is acceptable. Mirrors `filewrite.validate_relpath` —
 *  and mirrors it *loosely on purpose*: this is a UX filter, and the server is the authority. If
 *  the two ever disagree the server wins and the row shows what it said.
 */
export function relpathReason(relpath: string): string | null {
  if (!relpath) return "empty path";
  if (relpath.startsWith("/") || relpath.startsWith("\\")) return "absolute path";
  if (hasControlChar(relpath)) return "control characters in the name";
  const parts = relpath.split("/");
  if (parts.length > MAX_DEPTH) return `nested deeper than ${MAX_DEPTH} folders`;
  for (const p of parts) {
    if (p === "" || p === "." || p === "..") return "traversal segment in the path";
    if (p === ".git") return "git metadata — refused";
    if (p.includes("\\")) return "backslash in a name";
    if (new TextEncoder().encode(p).length > 255) return "name too long";
  }
  return null;
}

/** Turn a picked/dropped set into a plan, applying every bound the server also applies.
 *
 *  A per-file problem excludes **that file** and nothing else — a single 26 MB file in a 40-file
 *  drop must be one red row, not a lost upload. Only a whole-batch bound (too many files, too
 *  many bytes) refuses the batch, and it does so with the limit named.
 */
export function planUpload(files: PlannedFile[]): UploadPlan {
  const kept: PlannedFile[] = [];
  const rejected: { relpath: string; reason: string }[] = [];
  // Two dropped roots can normalize to the same relative path. Collapsing them silently let two
  // sources race one destination — the first completion settled the entry and the sibling lost
  // its retry — so a duplicate is a rejected row, not a quiet merge.
  const seen = new Set<string>();
  for (const f of files) {
    if (seen.has(f.relpath)) {
      rejected.push({ relpath: f.relpath, reason: "DUPLICATE — appears twice in this drop" });
      continue;
    }
    seen.add(f.relpath);
    const reason = relpathReason(f.relpath);
    if (reason) {
      rejected.push({ relpath: f.relpath, reason });
      continue;
    }
    if (f.size < 0) {
      rejected.push({ relpath: f.relpath, reason: "UNREADABLE — the browser would not open it" });
      continue;
    }
    if (f.size > MAX_FILE_BYTES) {
      rejected.push({
        relpath: f.relpath,
        reason: `TOO LARGE — ${mb(f.size)} MB > ${mb(MAX_FILE_BYTES)} MB`,
      });
      continue;
    }
    kept.push(f);
  }
  const bytes = kept.reduce((n, f) => n + f.size, 0);
  let refusal: string | null = null;
  if (kept.length > MAX_BATCH_FILES) {
    refusal = `That is ${kept.length} files — this panel takes at most ${MAX_BATCH_FILES} in one drop.`;
  } else if (bytes > MAX_BATCH_BYTES) {
    refusal = `That drop is ${mb(bytes)} MB — the limit is ${mb(MAX_BATCH_BYTES)} MB.`;
  } else if (!kept.length && !rejected.length) {
    refusal = "Nothing to upload.";
  }
  return { files: kept, rejected, bytes, refusal };
}

// --------------------------------------------------------------------------- folder walks

/** The minimal slice of the non-standard entry API this walk needs. */
export interface FsEntryLike {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file?: (cb: (f: File) => void, err?: (e: unknown) => void) => void;
  createReader?: () => {
    readEntries: (cb: (e: FsEntryLike[]) => void, err?: (e: unknown) => void) => void;
  };
}

/** Read a directory entry **completely**.
 *
 *  The gotcha this function exists for: Chromium's `readEntries` returns **at most 100 entries
 *  per call** and signals "done" with an empty array — so the obvious single call silently
 *  uploads the first 100 children of a folder and reports success. That is exactly the quiet
 *  truncation this panel's honesty rule forbids, so the loop runs until it comes back empty.
 */
export function readAllEntries(reader: {
  readEntries: (cb: (e: FsEntryLike[]) => void, err?: (e: unknown) => void) => void;
}): Promise<FsEntryLike[]> {
  return new Promise((resolve, reject) => {
    const all: FsEntryLike[] = [];
    const pump = () => {
      reader.readEntries((batch) => {
        if (!batch.length) {
          resolve(all);
          return;
        }
        all.push(...batch);
        pump();
      }, reject);
    };
    pump();
  });
}

/** Walk a dropped entry tree into flat `PlannedFile`s, preserving structure.
 *
 *  `base` is the relpath prefix accumulated so far, so `src/lib/util.ts` survives the drop.
 *  Bounded by `MAX_BATCH_FILES` so a mis-dropped `node_modules` stops the walk instead of
 *  spending minutes enumerating a tree that would be refused anyway.
 */
export async function walkEntry(
  entry: FsEntryLike,
  base = "",
  out: PlannedFile[] = [],
): Promise<PlannedFile[]> {
  // Stopping at exactly MAX_BATCH_FILES produced a plan of exactly MAX_BATCH_FILES, which is
  // NOT `> MAX_BATCH_FILES`, so `planUpload`'s refusal never fired and a 501-file folder
  // uploaded its first 500 in silence. The walk collects ONE past the limit precisely so the
  // refusal has something to see — the honest-truncation rule this panel is built on.
  if (out.length > MAX_BATCH_FILES) return out;
  const relpath = base ? `${base}/${entry.name}` : entry.name;
  if (entry.isFile && entry.file) {
    const file = await new Promise<File | null>((res) => {
      entry.file!(
        (f) => res(f),
        () => res(null),
      );
    });
    if (file) out.push({ file, relpath, size: file.size });
    // A file the browser refused to hand over is REPORTED, not dropped. Size 0 with an
    // unreadable marker makes it a rejected row rather than a file that quietly never existed.
    else out.push({ file: new File([], relpath.split("/").pop() ?? "?"), relpath, size: -1 });
    return out;
  }
  if (entry.isDirectory && entry.createReader) {
    const children = await readAllEntries(entry.createReader());
    for (const child of children) {
      if (out.length > MAX_BATCH_FILES) break;
      await walkEntry(child, relpath, out);
    }
  }
  return out;
}

/** The minimal `DataTransfer` slice a drop needs — typed loosely so a test can supply one. */
export interface DataTransferLike {
  items?: ArrayLike<{ kind?: string; webkitGetAsEntry?: () => FsEntryLike | null }>;
  files?: ArrayLike<File>;
}

/** Flatten a drop into `PlannedFile`s, preserving folder structure where the browser allows it.
 *
 *  `webkitGetAsEntry` is the only way a *dragged* folder can be read at all — `dataTransfer.files`
 *  lists a dropped directory as a zero-byte entry with no children. Where the entry API is
 *  missing the fallback takes the flat file list, which is the honest degradation: the files
 *  land, the folders do not, and nothing pretends otherwise.
 *
 *  **The entries must be captured synchronously.** A `DataTransferItemList` is emptied when the
 *  drop event handler returns, so calling `webkitGetAsEntry()` after the first `await` yields
 *  null for every item — a folder drop that silently produces nothing.
 */
export async function filesFromDataTransfer(dt: DataTransferLike): Promise<PlannedFile[]> {
  const entries: FsEntryLike[] = [];
  const items = dt.items ? Array.from(dt.items) : [];
  for (const item of items) {
    if (item?.kind && item.kind !== "file") continue;
    const entry = item?.webkitGetAsEntry?.();
    if (entry) entries.push(entry);
  }
  if (entries.length) {
    const out: PlannedFile[] = [];
    for (const e of entries) {
      // `>` not `>=`, for the same reason `walkEntry` uses it: stopping AT the ceiling returns
      // exactly MAX, which is not `> MAX`, so the refusal never fires. Dropping one 500-file
      // folder plus a second item silently omitted the second one.
      if (out.length > MAX_BATCH_FILES) break;
      await walkEntry(e, "", out);
    }
    return out;
  }
  return Array.from(dt.files ?? []).map((file) => ({
    file,
    relpath: file.name,
    size: file.size,
  }));
}

/** Does this browser have the folder picker at all?
 *
 *  iOS Safari does not. The control says so **with the reason** rather than offering a
 *  *Folder…* button that quietly produces one flattened file — which is the failure mode that
 *  makes a user distrust everything else the panel says.
 */
export function supportsFolderPicker(doc: Document = document): boolean {
  return "webkitdirectory" in doc.createElement("input");
}
