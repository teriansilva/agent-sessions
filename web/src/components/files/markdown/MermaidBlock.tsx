import { useEffect, useRef, useState } from "react";
import { useNear } from "./useNear";
import styles from "./markdownDocument.module.css";

/** A ```mermaid fence as a diagram (#2114).
 *
 *  Mermaid is its own lazy chunk family, imported the first time a diagram comes near the
 *  viewport, and kept out of the PWA precache (`pwaLazyChunks.ts`). The rendered SVG is NEVER put
 *  into the page: it is shown as `<img src="blob:…">`, where an SVG can run no script and load
 *  nothing — so even a Mermaid sanitiser bug cannot reach the app. `securityLevel: "strict"` is
 *  set as well, as the first line rather than the only one.
 *
 *  Labels are drawn as SVG text (no `foreignObject`), in the system sans the `<img>` can also see,
 *  so the boxes Mermaid measures in the page fit the text the image paints. Colours are read from
 *  the theme tokens where the preview sits, at render time. */

type Mermaid = typeof import("mermaid").default;

let loading: Promise<Mermaid> | null = null;
function loadMermaid(): Promise<Mermaid> {
  loading ??= import("mermaid").then((m) => m.default);
  return loading;
}

// Mermaid keeps global config and a scratch element per render, so renders run one at a time.
let queue: Promise<unknown> = Promise.resolve();
let seq = 0;

const FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

/** A token's resolved colour where `host` sits (`rgb(…)`), so a light-scoped preview gets light
 *  diagram colours. */
function tokenColor(host: Element, token: string, fallback: string): string {
  const probe = document.createElement("span");
  probe.style.color = `var(${token}, ${fallback})`;
  host.appendChild(probe);
  const c = getComputedStyle(probe).color || fallback;
  probe.remove();
  return c;
}

/** The SVG with an explicit size from its viewBox, so the `<img>` has an intrinsic size and scales
 *  down to the column instead of collapsing to 300 × 150. */
function sized(svg: string): string {
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  const root = doc.documentElement;
  if (root.nodeName !== "svg") return svg;
  const vb = (root.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
  if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) {
    root.setAttribute("width", String(Math.ceil(vb[2])));
    root.setAttribute("height", String(Math.ceil(vb[3])));
    root.style.removeProperty("max-width");
  }
  return new XMLSerializer().serializeToString(root);
}

async function render(host: Element, source: string): Promise<string> {
  const mermaid = await loadMermaid();
  const id = `md-mermaid-${++seq}`;
  const text = tokenColor(host, "--text", "#e8e9ec");
  const muted = tokenColor(host, "--text-muted", "#a7abb3");
  const node = tokenColor(host, "--surface-2", "#1a1c21");
  const ground = tokenColor(host, "--surface-3", "#08090b");
  const edge = tokenColor(host, "--line-strong", "#444");
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    fontFamily: FONT,
    htmlLabels: false,
    flowchart: { htmlLabels: false },
    maxTextSize: 50_000,
    themeVariables: {
      fontFamily: FONT,
      background: ground,
      primaryColor: node,
      primaryTextColor: text,
      primaryBorderColor: muted,
      secondaryColor: node,
      tertiaryColor: ground,
      lineColor: muted,
      textColor: text,
      mainBkg: node,
      nodeBorder: muted,
      clusterBkg: ground,
      clusterBorder: edge,
      edgeLabelBackground: ground,
      noteBkgColor: node,
      noteTextColor: text,
      noteBorderColor: muted,
      actorBkg: node,
      actorBorder: muted,
      actorTextColor: text,
      signalColor: text,
      signalTextColor: text,
    },
  });
  try {
    const { svg } = await mermaid.render(id, source);
    return sized(svg);
  } finally {
    // A failed render can leave its scratch element behind in <body>.
    document.getElementById(id)?.remove();
    document.getElementById(`d${id}`)?.remove();
  }
}

export function MermaidBlock({ source }: { source: string }) {
  const { ref, near } = useNear<HTMLDivElement>();
  const [state, setState] = useState<
    { k: "waiting" } | { k: "ok"; url: string } | { k: "error"; message: string }
  >({ k: "waiting" });
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    if (!near) return;
    const host = ref.current;
    if (!host) return;
    let live = true;
    const job = queue.then(() => (live ? render(host, source) : null));
    queue = job.catch(() => undefined);
    job
      .then((svg) => {
        if (!live || svg === null) return;
        const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
        urlRef.current = url;
        setState({ k: "ok", url });
      })
      .catch((e: unknown) => {
        if (!live) return;
        const message = e instanceof Error && e.message ? e.message : "Mermaid could not render this diagram.";
        setState({ k: "error", message: message.split("\n").slice(0, 6).join("\n") });
      });
    return () => {
      live = false;
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    };
  }, [near, ref, source]);

  const kind = /^\s*([A-Za-z-]+)/.exec(source)?.[1] ?? "";
  return (
    <div ref={ref} className={styles.block} data-md-mermaid={state.k}>
      <div className={styles.blockHead}>
        <span>{state.k === "error" ? "Diagram // Could not render" : "Diagram // Mermaid"}</span>
        <span>{kind}</span>
      </div>
      {state.k === "ok" ? (
        <div className={styles.diagram}>
          <img src={state.url} alt={`${kind || "Mermaid"} diagram`} />
        </div>
      ) : state.k === "error" ? (
        <>
          <p className={styles.blockError} role="status">
            {state.message}
          </p>
          <pre className={styles.blockCode}>
            <code>{source}</code>
          </pre>
        </>
      ) : (
        <p className={styles.blockWait} role="status">
          Drawing the diagram…
        </p>
      )}
    </div>
  );
}
