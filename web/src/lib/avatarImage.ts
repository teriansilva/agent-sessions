/** Prepare an avatar in the browser (#1931): centre-crop to a square, scale to `AVATAR_PX`, encode
 *  as WebP (PNG where the browser can't). The server re-checks the bytes and the size whatever
 *  this produces; this only keeps an upload small and square. */

export const AVATAR_PX = 256;
export const AVATAR_ACCEPT = "image/png,image/jpeg,image/webp";

export class AvatarImageError extends Error {}

/** The square crop of a `w`×`h` image: `[sx, sy, side]`. */
export function squareCrop(w: number, h: number): [number, number, number] {
  const side = Math.min(w, h);
  return [Math.floor((w - side) / 2), Math.floor((h - side) / 2), side];
}

export async function squareAvatar(file: Blob): Promise<Blob> {
  if (!/^image\/(png|jpeg|webp)$/.test(file.type))
    throw new AvatarImageError("Choose a PNG, JPEG or WebP image.");
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new AvatarImageError("That image can’t be read — try another file.");
  }
  try {
    const [sx, sy, side] = squareCrop(bitmap.width, bitmap.height);
    const out = Math.min(AVATAR_PX, side);
    const canvas = document.createElement("canvas");
    canvas.width = out;
    canvas.height = out;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new AvatarImageError("This browser can’t prepare the image.");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bitmap, sx, sy, side, side, 0, 0, out, out);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/webp", 0.9),
    );
    if (!blob) throw new AvatarImageError("This browser can’t prepare the image.");
    return blob; // `image/png` where WebP encoding isn't supported: toBlob falls back by itself
  } finally {
    bitmap.close();
  }
}
