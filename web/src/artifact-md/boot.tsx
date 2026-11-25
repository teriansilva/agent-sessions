import { createRoot } from "react-dom/client";
import { ArtifactMarkdown } from "../components/files/markdown/ArtifactMarkdown";

/** The hosted Markdown page's renderer (#2210). The server's page (`routes/artifacts.py`,
 *  `_markdown_page`) is complete without it: a header with Download above the file's text in a
 *  `<pre>`. This reads that text back (`textContent` undoes the server's HTML escaping), renders it
 *  beside the `<pre>`, and shows the Rendered / Source switch. `body[data-md]` is the page's state:
 *  `text` (the server's page as served), `rendered`, `source`, `big` (over the server's render
 *  limit: no script is sent) or `failed` (the script tag's `onerror`). */
export function boot(doc: Document): void {
  const pre = doc.getElementById("md-source");
  const mount = doc.getElementById("md-doc");
  const sw = doc.getElementById("md-switch");
  if (!pre || !mount || doc.body.dataset.md !== "text") return;
  const path = "/" + (pre.dataset.path ?? "");
  createRoot(mount).render(<ArtifactMarkdown path={path} text={pre.textContent ?? ""} />);
  const show = (view: "rendered" | "source") => {
    doc.body.dataset.md = view;
    sw?.querySelectorAll<HTMLButtonElement>("button[data-view]").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.view === view));
    });
  };
  sw?.addEventListener("click", (e) => {
    const b = (e.target as Element | null)?.closest<HTMLButtonElement>("button[data-view]");
    if (b) show(b.dataset.view === "source" ? "source" : "rendered");
  });
  if (sw) sw.hidden = false;
  show("rendered");
}
