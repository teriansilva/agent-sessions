import "./artifactMd.css";
import { boot } from "./boot";

/** The hosted Markdown renderer's entry (#2210), built by `vite.config.artifact-md.ts` as ONE
 *  classic script (IIFE): the page runs in an opaque-origin sandbox, where a module script, and
 *  every chunk it imports, is a CORS request BattleLab never answers. No chunks, no dynamic
 *  import, no app code beyond the Markdown seam; `artifactMdBundle.test.ts` keeps it that way. */
boot(document);
