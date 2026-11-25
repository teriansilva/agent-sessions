/** Plays a WAV preview through Web Audio (#1397): a blob: URL would need a `media-src` the CSP
 *  does not grant. */
export async function playWav(blob: Blob): Promise<void> {
  const ctx = new AudioContext();
  try {
    const buf = await ctx.decodeAudioData(await blob.arrayBuffer());
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    await new Promise<void>((resolve) => {
      src.onended = () => resolve();
      src.start();
    });
  } finally {
    void ctx.close();
  }
}

/** A language's English name ("de" → "German"), or the code when the browser doesn't know it. */
export function languageName(code: string): string {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code.toUpperCase();
  } catch {
    return code.toUpperCase();
  }
}
