/** The icon each shelf kind wears (#1740) — on its tile, its chip and the empty state. */
import {
  AlignLeft,
  AudioLines,
  FileText,
  Film,
  GitCommitHorizontal,
  Image as ImageIcon,
  Link as LinkIcon,
  type LucideIcon,
} from "lucide-react";

import type { ShelfKind } from "../../../types/shelf";

export const KIND_ICON: Record<ShelfKind, LucideIcon> = {
  image: ImageIcon,
  video: Film,
  audio: AudioLines,
  document: FileText,
  code: GitCommitHorizontal,
  link: LinkIcon,
  text: AlignLeft,
};
