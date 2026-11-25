import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
    const calls = serve({
      title: "Q4 latency report",
      kind: "site",
      updated_at: Date.now() / 1000 - 7200,
    });
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
    expect(screen.getByTestId("public-bar-brand")).toHaveAttribute(
      "href",
      "https://battlelabos.com/",
    );
    // The title is set by a passive effect after the metadata lands; wait for it.
    await waitFor(() =>
      expect(document.title).toBe("Q4 latency report · BattleLab"),
    );
    // One read, cookie-less.
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/artifacts/p/${ID}/~meta`);
    expect(calls[0].init?.credentials).toBe("omit");
  });

  test("a revoked or unknown link: 'no longer works', and nothing is framed", async () => {
    serve({ detail: "Not Found" }, 404);
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-unavailable")).toHaveTextContent(
      "This link no longer works",
    );
    expect(screen.queryByTestId("artifact-frame")).not.toBeInTheDocument();
  });

  test("the box can't be reached: 'try again', not 'gone'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network");
      }),
    );
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-offline")).toBeInTheDocument();
  });

  test("an address that isn't a public artifact link asks for nothing", async () => {
    const calls = serve({});
    at("/artifacts/p/short/");
    render(<ArtifactViewer />);
    expect(
      await screen.findByTestId("artifact-unavailable"),
    ).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  test("a password link (#2217): the card and nothing of the artifact; the right password frames it from ~u/<token>/", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    let tries = 4;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url.endsWith("~meta"))
          return new Response(
            JSON.stringify({ password_required: true, header: true }),
            { status: 401 },
          );
        const pw = JSON.parse(String(init?.body)).password;
        if (pw !== "right")
          return new Response(
            JSON.stringify({ password_required: true, tries_left: --tries }),
            { status: 401 },
          );
        return new Response(
          JSON.stringify({
            title: "Q4 latency report",
            kind: "site",
            updated_at: 0,
            header: true,
            token: "tok.en-1",
          }),
        );
      }),
    );
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    const gate = await screen.findByTestId("artifact-password-gate");
    expect(gate).toHaveTextContent("This link needs a password");
    expect(screen.getByTestId("public-bar-brand")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-frame")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Wrong password. 3 tries left before a 15-minute pause.",
    );
    await userEvent.clear(screen.getByLabelText("Password"));
    await userEvent.type(screen.getByLabelText("Password"), "right");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const frame = await screen.findByTestId("artifact-frame");
    expect(frame).toHaveAttribute("src", `/artifacts/p/${ID}/~u/tok.en-1/`);
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(screen.getByText("Q4 latency report")).toBeInTheDocument();
    const unlocks = calls.filter((c) => c.url.endsWith("~unlock"));
    expect(unlocks).toHaveLength(2);
    expect(unlocks[0].init?.method).toBe("POST");
    expect(unlocks[0].init?.credentials).toBe("omit");
    // The token lives in memory only.
    expect(window.sessionStorage.length).toBe(0);
    expect(window.location.pathname).toBe(`/artifacts/p/${ID}/`);
  });

  test("too many wrong passwords: the pause, in words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("~meta")
          ? new Response(
              JSON.stringify({ password_required: true, header: true }),
              { status: 401 },
            )
          : new Response(JSON.stringify({ retry_after: 900 }), { status: 429 }),
      ),
    );
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    await userEvent.type(await screen.findByLabelText("Password"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many wrong passwords. Try again in 15 minutes.",
    );
  });

  test("a link shared without the header: the artifact is the whole page, under its own title", async () => {
    serve({
      title: "Q4 latency report",
      kind: "file",
      updated_at: 0,
      header: false,
    });
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    const frame = await screen.findByTestId("artifact-frame");
    expect(frame).toHaveAttribute("src", `/artifacts/p/${ID}/~/`);
    expect(screen.queryByTestId("public-bar-brand")).not.toBeInTheDocument();
    expect(screen.getByTestId("artifact-viewer")).toHaveAttribute(
      "data-header",
      "off",
    );
    await waitFor(() => expect(document.title).toBe("Q4 latency report"));
  });

  test("a locked link without the header: the password card alone, no bar", async () => {
    serve({ password_required: true, header: false }, 401);
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(
      await screen.findByTestId("artifact-password-gate"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("public-bar-brand")).not.toBeInTheDocument();
  });

  test("while the first read is in flight no bar is drawn (a headerless link never flashes one)", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    at(`/artifacts/p/${ID}/`);
    render(<ArtifactViewer />);
    expect(screen.getByTestId("artifact-viewer")).toHaveAttribute(
      "data-phase",
      "loading",
    );
    expect(screen.queryByTestId("public-bar-brand")).not.toBeInTheDocument();
  });
});

describe("ArtifactViewer — the operator's own view (#2270)", () => {
  const TOKEN = "eyJhIjoiMTIzNC01Njc4LTkwMTItMzQ1NiJ9.Zz-_9.s1g";

  test("always wears the header, frames the artifact under the view token, reads cookie-less", async () => {
    // Even a meta that says header:false: hiding the header is a share option, not this view's.
    const calls = serve({
      title: "Weekly build report",
      kind: "site",
      updated_at: Date.now() / 1000 - 3600,
      header: false,
    });
    at(`/artifacts/o/${TOKEN}/?tab=2#top`);
    render(<ArtifactViewer />);
    const frame = await screen.findByTestId("artifact-frame");
    expect(frame).toHaveAttribute("src", `/artifacts/v/${TOKEN}/?tab=2#top`);
    expect(frame.getAttribute("sandbox")!.split(" ")).not.toContain(
      "allow-same-origin",
    );
    expect(screen.getByTestId("artifact-viewer")).toHaveAttribute(
      "data-header",
      "on",
    );
    expect(screen.getByTestId("public-bar")).toBeInTheDocument();
    expect(screen.getByText("Weekly build report")).toBeInTheDocument();
    expect(screen.getByText("SITE")).toBeInTheDocument();
    await waitFor(() =>
      expect(document.title).toBe("Weekly build report · BattleLab"),
    );
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`/artifacts/o/${TOKEN}/~meta`);
    expect(calls[0].init?.credentials).toBe("omit");
  });

  test("an expired view says to open the artifact again, and frames nothing", async () => {
    serve({ detail: "expired" }, 404);
    at(`/artifacts/o/${TOKEN}/`);
    render(<ArtifactViewer />);
    expect(await screen.findByTestId("artifact-unavailable")).toHaveTextContent(
      "This view has expired",
    );
    expect(screen.getByTestId("public-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("artifact-frame")).not.toBeInTheDocument();
  });
});
