/** Where a playbook copy came from (#2144), for its card's source tag and provenance line and its
 *  detail's Source fact. */
import type { ReactNode } from "react";
import { Box, GalleryHorizontal } from "lucide-react";
import type { PlaybookCardData } from "../../types/playbooks";
import type { LibrarySource } from "../library/LibraryCard";
import { ImportedMark } from "../share/ShareDialogs";

/** What a playbook copy IS (#2144): bundled and catalog by its store; a local one by where it came
 *  from — the public gallery (its recorded origin), a file (the share-import ledger) or this host. */
export function playbookSource(card: PlaybookCardData, fromFile: boolean): LibrarySource {
  if (card.source !== "local") return card.source;
  if (card.gallery_origin) return "gallery";
  return fromFile ? "imported" : "local";
}

/** The provenance line a card's footer and a detail's Source fact share. */
export function playbookProvenance(card: PlaybookCardData, source: LibrarySource): ReactNode {
  switch (source) {
    case "gallery":
      return (
        <>
          <GalleryHorizontal size={14} aria-hidden />
          From the gallery
          {card.gallery_origin?.publisher && (
            <>
              {" · "}
              <b>{card.gallery_origin.publisher}</b>
            </>
          )}
        </>
      );
    case "imported":
      return <ImportedMark />;
    case "bundled":
      return (
        <>
          <Box size={14} aria-hidden />
          Shipped with this release
        </>
      );
    case "catalog":
      return "Catalog · trust not reported";
    default:
      return "Unsigned · made on this host";
  }
}

