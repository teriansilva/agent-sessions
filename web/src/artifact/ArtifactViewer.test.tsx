import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { ArtifactViewer } from "./ArtifactViewer";

const ID = "Ar7fQ2xAAAAAAAAAAAAAAA";

function at(path: string) {
  window.history.replaceState(null, "", path);
}

function serve(body: unknown, status = 200) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  at("/");
});

describe("ArtifactViewer (#2068)", () => {
  test("a live link: the header with its title and kind, the artifact framed from ~/ in a sandbox", async () => {
    const calls = serve({ title: "Q4 latency report", kind: "site", updated_at: Date.now() / 1000 - 7200 });
    at(`/artifacts/p/${ID}/?tab=2#p95`);
    render(<ArtifactViewer />);
    const frame = await screen.findByTestId("artifact-frame");
    // The query and fragment it was opened with travel to the content.
    expect(frame).toHaveAttribute("src", `/artifacts/p/${ID}/~/?tab=2#p95`);
    const tokens = frame.getAttribute("sandbox")!.split(" ");
    expect(tokens).toContain("allow-scripts");
    expect(tokens).not.toContain("allow-same-origin");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(screen.getByText("Q4 latency report")).toBeInTheDocument();
    expect(screen.getByText("SITE")).toBeInTheDocument();
    expect(screen.getByText("UPDATED 2 H AGO")).toBeInTheDocument();
    expect(screen.getByTestId("public-bar-brand")).toHaveAttribute("href", "https://battlelabos.com/");
    expect(document.title).toBe("Q4 latency report · BattleLab");
    // One read, cookie-less.
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/artifacts/p/${ID}/~meta`);
    expect(calls[0].init?.credentials).toBe("omit");
  });

  test("a revoked or unknown link: 'no longer works', and nothing is framed", async () => {
    serve({ detail: "Not Found" }, 404);
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-unavailable")).toHaveTextContent("This link no longer works");
    expect(screen.queryByTestId("artifact-frame")).not.toBeInTheDocument();
  });

  test("the box can't be reached: 'try again', not 'gone'", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-offline")).toBeInTheDocument();
  });

  test("an address that isn't a public artifact link asks for nothing", async () => {
    const calls = serve({});
    at("/artifacts/p/short/");
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-unavailable")).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });
});
