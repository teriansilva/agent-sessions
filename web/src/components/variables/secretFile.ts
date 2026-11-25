/** The largest secret file the store takes (`vault_files.MAX_BYTES`). */
export const SECRET_FILE_MAX = 256 * 1024;

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}
