import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { commonMocks } from "./mission-directions";
import { settingsPath } from "../src/routes/settingsTabs";
import type {
  FederationPairing,
  FederationPeer,
  FederationState,
} from "../src/lib/federation";
import { TOUCH_FLOOR } from "./touchTarget";

// Settings → Peers (#1246 P2, #2052) in a real browser, on both projects, against a small in-test
// model of `routes/federation.py`. What breaks here is what jsdom cannot see: where focus lands
// when the code card arrives, whether a phone-width row keeps 44 px targets and its width, whether
// the poll really stops on a hidden page. The two-real-instances run is settings-peers-real.spec.ts.

const NOW = () => Date.now() / 1000;
const INVITE =
  "blfed1:mfzwizlomvzsa5dimuqhg33nmuqhi2djonuw4zzamjswk3ramfxgiidun5qxi2dfebuw45dfon2ca33gmjswg5dpojxgk4q7n2bxd5mr4tzh6pa";
const GPU = "m5tr2kxwq7ahd3ny6bpev4czj2osu7fl";
const LAPTOP = "b7nd4qzse2vkx6mhp3ytw5caj4ruo2gf";
const OLD = "t2hy6wfnc5qdk7ezr3mbx4sav6pju2lo";
const NEW_PEER = "c4vy2sxqe7kdm3nzh6pbw5taj2ruo7gf";
const NOT_OWNER_DETAIL = "another BattleLab process on this store is running federation";
const NOT_OWNER_NOTICE =
  "Another BattleLab process on this machine is running federation. Pairing happens there. You can still revoke a peer here.";
const GONE_NOT_STORED = "This pairing is no longer open. The instance is not in your list of peers.";
const GONE_STORED = "This pairing is no longer open. The instance is in your list of peers.";
const INVITE_GONE =
  "This invite is no longer open. If the other instance used it, check your list of peers.";

const peer = (over: Partial<FederationPeer> & { peer_id: string; name: string }): FederationPeer => ({
  state: "trusted",
  connected: false,
  refused: false,
  reachable_by: ["direct"],
  created_at: NOW() - 9 * 86400,
  last_seen_at: NOW() - 2 * 3600,
  revision: 1,
  ...over,
});

const threePeers = (): FederationPeer[] => [
  peer({ peer_id: GPU, name: "gpu-box", connected: true, last_seen_at: NOW() - 10 }),
  peer({ peer_id: LAPTOP, name: "laptop" }),
  peer({ peer_id: OLD, name: "old-desktop", state: "revoked" }),
];

interface Write {
  method: string;
  path: string;
  body: unknown;
  csrf: string | undefined;
}

/** The server, as far as this page sees it. */
class FakeFederation {
  state: FederationState = {
    owner: true,
    self: { peer_id: "k3wq7n2bxd5mr4tzh6pac2vyj7uef3so", name: "studio", reachable_by: ["direct"] },
    peers: threePeers(),
    invites: [],
    pairings: [],
  };
  reads = 0;
  writes: Write[] = [];
  /** Answer the next write matching this `METHOD /suffix` with a refusal instead. */
  refuse: Record<string, { status: number; detail: string; code: string; then?: () => void }> = {};

  pairing(over: Partial<FederationPairing> = {}): FederationPairing {
    return {
      id: "pair0001",
      role: "acceptor",
      state: "dialing",
      code: "",
      peer_id: NEW_PEER,
      name: "",
      local_confirmed: false,
      remote_confirmed: false,
      expires_at: NOW() + 600,
      error: "",
      ...over,
    };
  }
  /** The handshake finished: both sides now show the code. */
  reachCode(id = "pair0001") {
    const p = this.state.pairings.find((x) => x.id === id)!;
    Object.assign(p, { state: "confirm", code: "482915", name: "render-box" });
  }
  /** The other operator confirmed too: the peer is stored. */
  finish(id = "pair0001") {
    const p = this.state.pairings.find((x) => x.id === id)!;
    Object.assign(p, { state: "done", code: "", remote_confirmed: true });
    this.state.peers.push(
      peer({ peer_id: NEW_PEER, name: "render-box", connected: true, last_seen_at: NOW() }),
    );
  }

  async handle(route: Route) {
    const req = route.request();
    const method = req.method();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/federation/, "") || "/";
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    // What is shared with and by each peer (#2055) has its own reads; share-peer.spec.ts covers
    // them. Here nothing is shared, and these reads are not the page's own poll (`reads`).
    if (method === "GET" && path.startsWith("/grants")) return json({ grants: [], generation: 0 });
    if (method === "GET" && path.startsWith("/remote")) return json({ remote: [] });
    if (method === "GET") {
      this.reads++;
      return json(this.state);
    }
    this.writes.push({
      method,
      path,
      body: req.postDataJSON(),
      csrf: req.headers()["x-csrf-token"],
    });
    const refusal = this.refuse[`${method} ${path}`];
    if (refusal) {
      delete this.refuse[`${method} ${path}`];
      refusal.then?.();
      return json({ detail: refusal.detail, code: refusal.code }, refusal.status);
    }
    const s = this.state;
    let m: RegExpMatchArray | null;
    if (method === "POST" && path === "/invites") {
      const made = { id: "inv00001", expires_at: NOW() + 600 };
      s.invites.push(made);
      return json({ ...made, invite: INVITE });
    }
    if (method === "DELETE" && (m = path.match(/^\/invites\/([^/]+)$/))) {
      s.invites = s.invites.filter((i) => i.id !== m![1]);
      return json(s);
    }
    if (method === "POST" && path === "/pairings") {
      const p = this.pairing();
      s.pairings.push(p);
      return json(p);
    }
    if (method === "POST" && (m = path.match(/^\/pairings\/([^/]+)\/(confirm|deny)$/))) {
      const p = s.pairings.find((x) => x.id === m![1]);
      if (!p) return json({ detail: "unknown pairing", code: "not_found" }, 404);
      if (m[2] === "confirm") p.local_confirmed = true;
      else Object.assign(p, { state: "denied", code: "" });
      return json(p);
    }
    if (method === "PATCH" && (m = path.match(/^\/peers\/([^/]+)$/))) {
      const p = s.peers.find((x) => x.peer_id === m![1])!;
      p.name = (req.postDataJSON() as { name: string }).name;
      return json(s);
    }
    if (method === "POST" && (m = path.match(/^\/peers\/([^/]+)\/revoke$/))) {
      const p = s.peers.find((x) => x.peer_id === m![1])!;
      Object.assign(p, { state: "revoked", connected: s.owner ? false : null });
      return json(s);
    }
    return json({ detail: "not mocked", code: "not_found" }, 404);
  }
}

async function open(page: Page, prepare?: (fake: FakeFederation) => void): Promise<FakeFederation> {
  const fake = new FakeFederation();
  prepare?.(fake);
  await commonMocks(page);
  await page.route(/\/api\/federation(\/.*)?(\?.*)?$/, (r) => fake.handle(r));
  await page.goto(settingsPath("peers"));
  await expect(page.getByTestId("peers-self")).toBeVisible();
  return fake;
}

const row = (page: Page, peerId: string) => page.locator(`[data-testid="peer-row"][data-peer="${peerId}"]`);

/** Nothing on the page is wider than the viewport, and the document does not scroll sideways. */
async function expectNoSidewaysOverflow(page: Page) {
  const over = await page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const root = document.querySelector('[data-testid="peers-settings"]')!;
    const wide = [...root.querySelectorAll<HTMLElement>("*")]
      .filter((el) => el.getBoundingClientRect().right > vw + 1)
      .map((el) => `${el.tagName}.${el.className}`);
    return { doc: document.documentElement.scrollWidth - vw, wide };
  });
  expect(over.wide).toEqual([]);
  expect(over.doc).toBeLessThanOrEqual(1);
}

/** Every control the operator can press is at least 44×44 where a finger is the pointer. */
async function expectTouchTargets(scope: Locator) {
  const controls = scope.locator("button:visible, input:visible, a:visible");
  const n = await controls.count();
  expect(n).toBeGreaterThan(0);
  for (let i = 0; i < n; i++) {
    const control = controls.nth(i);
    const box = (await control.boundingBox())!;
    const what = await control.evaluate((el) => `${el.tagName} "${el.textContent || el.id}"`);
    expect(box.height, `${what} height`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
    expect(box.width, `${what} width`).toBeGreaterThanOrEqual(TOUCH_FLOOR);
  }
}

/** On the phone project: the layout holds its width and its targets in the state on screen. */
async function expectPhoneLayout(page: Page, isMobile: boolean, scope?: Locator) {
  await expectNoSidewaysOverflow(page);
  if (isMobile) await expectTouchTargets(scope ?? page.getByTestId("peers-settings"));
}

test("at rest: this instance, and three peers with a state that is a word and a colour", async ({
  page,
  isMobile,
}) => {
  await open(page);

  const self = page.getByTestId("peers-self");
  await expect(self.getByTestId("peer-id")).toHaveText("k3wq7n2bxd5mr4tzh6pac2vyj7uef3so");
  await expect(self.getByTestId("peer-id").locator("span")).toHaveText([
    "k3wq", "7n2b", "xd5m", "r4tz", "h6pa", "c2vy", "j7ue", "f3so",
  ]);
  await expect(page.getByTestId("peers-reachable")).toHaveText("directly");
  // How it is reachable is a kind. The page never shows an address, its own origin included.
  const text = await page.getByTestId("peers-settings").innerText();
  expect(text).not.toMatch(/https?:|wss?:|127\.0\.0\.1|localhost/i);

  await expect(page.getByRole("heading", { name: /Paired instances/ })).toContainText("3");
  await expect(row(page, GPU)).toHaveAttribute("data-state", "connected");
  await expect(row(page, GPU).getByTestId("peer-state")).toHaveText("connected");
  await expect(row(page, GPU).locator(".hud-led")).toHaveAttribute("data-tone", "up");
  await expect(row(page, LAPTOP).getByTestId("peer-state")).toHaveText("offline · last seen 2 h ago");
  await expect(row(page, LAPTOP).locator(".hud-led")).toHaveAttribute("data-tone", "idle");
  await expect(row(page, GPU)).toContainText("Reachable by: direct");

  // Revoked: listed, stamped, quiet, and with nothing to press.
  const old = row(page, OLD);
  await expect(old.locator(".nj-stamp")).toHaveText("Revoked");
  await expect(old).toContainText("Its key stays refused. Pair again to restore it.");
  await expect(old.getByRole("button")).toHaveCount(0);

  await expect(page.getByRole("button", { name: "Create invite" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Pair", exact: true })).toBeDisabled();
  await expectPhoneLayout(page, isMobile);
});

test("no peers yet: the list says so and how to start", async ({ page }) => {
  await open(page, (f) => (f.state.peers = []));
  await expect(page.getByTestId("peers-list")).toContainText("No paired instances yet.");
  await expect(page.getByRole("heading", { name: /Paired instances/ })).toContainText("0");
});

test("create an invite: the string, its QR and a countdown; Withdraw takes it back", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Create invite" }).click();

  const card = page.getByTestId("peers-invite");
  await expect(card.getByTestId("invite-string")).toHaveText(INVITE);
  await expect(card.getByTestId("invite-countdown")).toHaveText(/Valid for 9:5\d · works once/);
  await expect(card).toContainText("Send it to the other operator through a channel you trust.");
  expect(fake.writes[0]).toMatchObject({ method: "POST", path: "/invites", csrf: "x" });

  // The QR is drawn here, from the string: dark modules on a white ground with a quiet zone, in
  // whichever theme, and no image request went out for it.
  const qr = card.getByTestId("invite-qr");
  await expect(qr).toBeVisible();
  const drawn = await qr.evaluate((svg) => {
    const ground = getComputedStyle(svg.querySelector("rect")!).fill;
    const ink = getComputedStyle(svg.querySelector("path")!).fill;
    const d = svg.querySelector("path")!.getAttribute("d")!;
    const first = d.match(/^M(\d+) (\d+)/)!;
    return { ground, ink, modules: d.split("M").length - 1, x: Number(first[1]), y: Number(first[2]) };
  });
  expect(drawn.ground).toBe("rgb(255, 255, 255)");
  expect(drawn.ink).toBe("rgb(0, 0, 0)");
  expect(drawn.modules).toBeGreaterThan(100);
  expect(drawn.x).toBeGreaterThanOrEqual(4); // the quiet zone
  expect(drawn.y).toBeGreaterThanOrEqual(4);

  // The countdown is live.
  const before = await card.getByTestId("invite-countdown").innerText();
  await expect(card.getByTestId("invite-countdown")).not.toHaveText(before);

  // The invite is a secret: component state only.
  const leaked = await page.evaluate((needle) => {
    const stores = [localStorage, sessionStorage].map((s) => JSON.stringify({ ...s }));
    return stores.some((s) => s.includes(needle)) || location.href.includes(needle);
  }, "blfed1:");
  expect(leaked).toBe(false);

  await expectPhoneLayout(page, isMobile);

  await card.getByRole("button", { name: "Withdraw" }).click();
  await expect(card).toHaveCount(0);
  expect(fake.writes[1]).toMatchObject({ method: "DELETE", path: "/invites/inv00001", csrf: "x" });
  await expect(page.getByRole("button", { name: "Create invite" })).toBeEnabled();
});

test("accept an invite: contact, compare the code, confirm, wait, and the peer is listed", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();

  const card = page.getByTestId("peers-pairing");
  await expect(card).toContainText("Contacting the other instance…");
  await expect(card).toContainText("Nothing is stored until both operators confirm the same code.");
  expect(fake.writes[0]).toMatchObject({ method: "POST", path: "/pairings", body: { invite: INVITE } });

  fake.reachCode();
  const code = card.getByTestId("pairing-code");
  await expect(code.locator("span:not(.nj-sr-only)")).toHaveText(["482", "915"]);
  await expect(card).toContainText(
    "The other instance shows this code too. Confirm only if both screens show the same digits.",
  );
  await expect(card.getByTestId("pairing-name")).toHaveText("render-box");
  await expect(card.getByTestId("peer-id")).toHaveText(NEW_PEER);

  // The code is the biggest thing on the card, and confirming is a choice, never a default.
  const match = card.getByRole("button", { name: "They match" });
  await expect(match).toBeVisible();
  await expect(match).not.toBeFocused();
  const sizes = await card.evaluate((el) => {
    const px = (n: Element) => parseFloat(getComputedStyle(n).fontSize);
    const code = px(el.querySelector('[data-testid="pairing-code"]')!);
    const rest = [...el.querySelectorAll("p, dd, dt, button, h2 *")].map(px);
    return { code, rest: Math.max(...rest) };
  });
  expect(sizes.code).toBeGreaterThan(sizes.rest);
  // Enter with nothing chosen confirms nothing.
  await page.keyboard.press("Enter");
  expect(fake.writes).toHaveLength(1);
  await expectPhoneLayout(page, isMobile);

  await match.click();
  await expect(card.getByTestId("pairing-waiting")).toContainText(
    "Confirmed here · waiting for the other operator",
  );
  await expect(card.getByRole("button", { name: "They match" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Cancel pairing" })).toBeEnabled();
  expect(fake.writes[1]).toMatchObject({ method: "POST", path: "/pairings/pair0001/confirm" });
  // Not stored yet: the list still has the three it had.
  await expect(page.getByTestId("peer-row")).toHaveCount(3);
  await expectPhoneLayout(page, isMobile);

  fake.finish();
  await expect(row(page, NEW_PEER)).toHaveAttribute("data-state", "connected");
  await expect(row(page, NEW_PEER)).toContainText("render-box");
  await expect(card).toHaveCount(0);
  await expect(page.getByTestId("peers-paired")).toContainText("render-box is now a paired instance.");
  await expect(page.getByRole("button", { name: "Create invite" })).toBeEnabled();
});

test("the inviter's side: the invite is used, and its card becomes the code", async ({ page }) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Create invite" }).click();
  await expect(page.getByTestId("invite-string")).toBeVisible();

  // The other instance completed the handshake: the invite is spent, its pairing has its id.
  fake.state.invites = [];
  fake.state.pairings.push(
    fake.pairing({ id: "inv00001", role: "inviter", state: "confirm", code: "482915", name: "render-box" }),
  );
  await expect(page.getByTestId("pairing-code")).toContainText("482915");
  await expect(page.getByTestId("peers-invite")).toHaveCount(0);

  // Cancelling here is this operator's own act: the card leaves, with nothing to explain.
  await page.getByRole("button", { name: "Cancel pairing" }).click();
  await expect(page.getByTestId("peers-pairing")).toHaveCount(0);
  expect(fake.writes.at(-1)).toMatchObject({ method: "POST", path: "/pairings/inv00001/deny" });
  await expect(page.getByRole("button", { name: "Create invite" })).toBeVisible();
});

test("a failed pairing says the server's reason, and offers another try", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(page.getByTestId("peers-pairing")).toContainText("Contacting the other instance…");

  Object.assign(fake.state.pairings[0], {
    state: "failed",
    error: "the other instance refused this invite (used, expired or wrong)",
  });
  const settled = page.getByTestId("pairing-settled");
  await expect(settled).toHaveAttribute("role", "alert");
  await expect(settled).toContainText("Pairing failed");
  await expect(settled).toContainText(
    "The other instance refused this invite (used, expired or wrong). Nothing was stored.",
  );
  await expect(settled.locator(".hud-led")).toHaveAttribute("data-tone", "down");
  await expectPhoneLayout(page, isMobile);

  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("peers-pairing")).toHaveCount(0);
  await expect(page.getByLabel("Invite")).toBeVisible();
  // The server keeps listing the failed pairing for a while; it does not come back.
  const reads = fake.reads;
  await expect.poll(() => fake.reads, { timeout: 20_000 }).toBeGreaterThan(reads);
  await expect(page.getByTestId("peers-pairing")).toHaveCount(0);
});

test("an expired pairing is quiet, with the server's reason", async ({ page }) => {
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(page.getByTestId("peers-pairing")).toBeVisible();
  Object.assign(fake.state.pairings[0], { state: "expired", error: "the pairing timed out" });
  const settled = page.getByTestId("pairing-settled");
  await expect(settled).toContainText("The pairing timed out. Nothing was stored.");
  await expect(settled).toHaveAttribute("role", "status");
  await expect(settled.locator(".hud-led")).toHaveAttribute("data-tone", "idle");
  await expect(page.getByRole("button", { name: "Pair again" })).toBeVisible();
});

test("a refused invite is explained beside the field it was pasted into", async ({ page }) => {
  const fake = await open(page);
  fake.refuse["POST /pairings"] = {
    status: 422,
    detail: "this is not a BattleLab pairing invite",
    code: "bad_invite",
  };
  await page.getByLabel("Invite").fill("hello");
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  const error = page.locator("#peers-accept-error");
  await expect(error).toHaveText("this is not a BattleLab pairing invite");
  await expect(page.getByLabel("Invite")).toHaveAttribute("aria-describedby", "peers-accept-error");
  await expect(page.getByLabel("Invite")).toHaveValue("hello");
  await expect(page.getByTestId("peers-pairing")).toHaveCount(0);
});

test("rename is inline, local, and a refusal stays beside the field", async ({ page, isMobile }) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Rename gpu-box" }).click();

  const field = page.getByLabel("Name on this instance");
  await expect(field).toBeFocused();
  await expect(field).toHaveValue("gpu-box");
  await expect(row(page, GPU)).toContainText("Only a label here. The other instance does not see it.");
  await expectPhoneLayout(page, isMobile);

  fake.refuse[`PATCH /peers/${GPU}`] = { status: 422, detail: "name is too long", code: "invalid" };
  await field.fill("render-box");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(row(page, GPU).getByRole("alert")).toHaveText("name is too long");
  await expect(field).toHaveValue("render-box");

  await field.press("Enter");
  await expect(row(page, GPU).locator("span").filter({ hasText: /^render-box$/ })).toBeVisible();
  await expect(field).toHaveCount(0);
  expect(fake.writes.at(-1)).toMatchObject({
    method: "PATCH",
    path: `/peers/${GPU}`,
    body: { name: "render-box" },
    csrf: "x",
  });
  // Focus goes back to the row's own action, not to the top of the page.
  await expect(page.getByRole("button", { name: "Rename render-box" })).toBeFocused();

  // Escape leaves the name as it was.
  await page.getByRole("button", { name: "Rename laptop" }).click();
  await page.getByLabel("Name on this instance").fill("something else");
  await page.keyboard.press("Escape");
  await expect(row(page, LAPTOP)).toContainText("laptop");
  expect(fake.writes.filter((w) => w.method === "PATCH")).toHaveLength(2);
});

test("revoke asks first, with focus on Cancel, and the row settles as revoked", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Revoke gpu-box" }).click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("Revoke gpu-box?");
  await expect(dialog).toContainText(
    "It can no longer connect to this instance, and everything shared with it ends. You can pair again later.",
  );
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  if (isMobile) await expectTouchTargets(dialog);

  // Enter on the default focus cancels: nothing was revoked.
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveCount(0);
  expect(fake.writes).toHaveLength(0);
  await expect(row(page, GPU)).toHaveAttribute("data-state", "connected");

  await page.getByRole("button", { name: "Revoke gpu-box" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  expect(fake.writes[0]).toMatchObject({ method: "POST", path: `/peers/${GPU}/revoke`, csrf: "x" });
  await expect(row(page, GPU)).toHaveAttribute("data-state", "revoked");
  await expect(row(page, GPU).locator(".nj-stamp")).toHaveText("Revoked");
  await expect(row(page, GPU).getByRole("button")).toHaveCount(0);
});

test("a peer that removed this instance: amber, a word, and Remove", async ({ page }) => {
  const fake = await open(page, (f) => {
    f.state.peers = [peer({ peer_id: LAPTOP, name: "laptop", refused: true })];
  });
  const r = row(page, LAPTOP);
  await expect(r.getByTestId("peer-state")).toHaveText("removed by the other side");
  await expect(r.locator(".hud-led")).toHaveAttribute("data-tone", "att");
  await expect(r).toContainText(
    "This instance was removed by the other side. It no longer accepts a connection from here.",
  );
  // The peer's own actions (its shares have their own disclosure, #2055).
  await expect(r.getByTestId("peer-actions").getByRole("button")).toHaveText(["Remove"]);
  await r.getByRole("button", { name: "Remove laptop" }).click();
  await expect(page.getByRole("alertdialog")).toContainText("Remove laptop?");
  await page.getByRole("alertdialog").getByRole("button", { name: "Remove", exact: true }).click();
  await expect(r).toHaveAttribute("data-state", "revoked");
  expect(fake.writes[0]).toMatchObject({ method: "POST", path: `/peers/${LAPTOP}/revoke` });
});

test("no address: Create invite is off with the reason; accepting still works", async ({
  page,
  isMobile,
}) => {
  await open(page, (f) => (f.state.self.reachable_by = []));
  const notice = page.getByTestId("peers-no-address");
  await expect(notice).toContainText(
    "This instance has no address another instance can reach. Set a public address for it to create invites. You can still accept an invite from an instance that has one.",
  );
  await expect(notice.locator(".hud-led")).toHaveAttribute("data-tone", "att");
  await expect(page.getByTestId("peers-reachable")).toHaveText("no address");
  const create = page.getByRole("button", { name: "Create invite" });
  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("aria-describedby", "peers-no-address");
  await expectPhoneLayout(page, isMobile);

  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  await expect(page.getByTestId("peers-pairing")).toContainText("Contacting the other instance…");
});

test("a process that does not own federation: read-only, no connection claim, Revoke works", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page, (f) => {
    f.state.owner = false;
    for (const p of f.state.peers) p.connected = null;
  });
  await expect(page.getByTestId("peers-not-owner")).toContainText(NOT_OWNER_NOTICE);

  for (const id of [GPU, LAPTOP]) {
    await expect(row(page, id).getByTestId("peer-state")).toHaveText("connection not shown here");
    await expect(row(page, id).locator(".hud-led")).toHaveAttribute("data-tone", "idle");
    await expect(row(page, id).getByTestId("peer-actions").getByRole("button")).toHaveText(["Revoke"]);
  }
  // Neither claim is made anywhere in the list.
  const list = await page.getByTestId("peers-list").innerText();
  expect(list).not.toMatch(/\bconnected\b|\boffline\b|last seen/i);

  await expect(page.getByRole("button", { name: "Create invite" })).toBeDisabled();
  await expect(page.getByLabel("Invite")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Pair", exact: true })).toBeDisabled();
  await expectPhoneLayout(page, isMobile);

  await page.getByRole("button", { name: "Revoke gpu-box" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(row(page, GPU)).toHaveAttribute("data-state", "revoked");
  expect(fake.writes).toEqual([
    { method: "POST", path: `/peers/${GPU}/revoke`, body: null, csrf: "x" },
  ]);
});

test("409 not_owner in the middle of a flow: the reason on the card, then read-only", async ({
  page,
}) => {
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  fake.reachCode();
  const card = page.getByTestId("peers-pairing");
  await expect(card.getByRole("button", { name: "They match" })).toBeVisible();

  // Another process took federation over: this one answers 409, lists nothing in progress, and
  // no longer knows who is connected.
  fake.refuse["POST /pairings/pair0001/confirm"] = {
    status: 409,
    detail: NOT_OWNER_DETAIL,
    code: "not_owner",
    then: () => {
      fake.state.owner = false;
      fake.state.pairings = [];
      for (const p of fake.state.peers) p.connected = null;
    },
  };
  await card.getByRole("button", { name: "They match" }).click();

  await expect(card.getByRole("alert")).toHaveText(NOT_OWNER_DETAIL);
  await expect(card).toContainText(GONE_NOT_STORED);
  await expect(card.getByRole("button", { name: "They match" })).toHaveCount(0);
  await expect(page.getByTestId("peers-not-owner")).toContainText(NOT_OWNER_NOTICE);
  await expect(page.getByRole("button", { name: "Create invite" })).toBeDisabled();
  await expect(page.getByLabel("Invite")).toBeDisabled();
  await expect(row(page, GPU).getByTestId("peer-state")).toHaveText("connection not shown here");
  // Nothing was stored, and nothing claims it was.
  await expect(page.getByTestId("peer-row")).toHaveCount(3);
  await expect(page.getByTestId("peers-paired")).toHaveCount(0);

  await card.getByRole("button", { name: "Dismiss" }).click();
  await expect(card).toHaveCount(0);
});

test("a restart drops an open pairing: the card says so and offers a new start", async ({ page }) => {
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  fake.reachCode();
  const card = page.getByTestId("peers-pairing");
  await card.getByRole("button", { name: "They match" }).click();
  await expect(card.getByTestId("pairing-waiting")).toBeVisible();

  fake.state.pairings = []; // the server restarted: in-process state is gone
  await expect(card.getByTestId("pairing-settled")).toHaveText(new RegExp(`${GONE_NOT_STORED}$`));
  await expect(card.getByTestId("pairing-waiting")).toHaveCount(0);
  await expect(page.getByTestId("pairing-code")).toHaveCount(0);
  await expect(page.getByTestId("peer-row")).toHaveCount(3);
  await expect(page.getByTestId("peers-paired")).toHaveCount(0);

  await card.getByRole("button", { name: "Start again" }).click();
  await expect(card).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create invite" })).toBeEnabled();
  await expect(page.getByLabel("Invite")).toHaveValue("");
});

test("completed while this page was not reading, record swept: the card says the peer IS stored", async ({
  page,
}) => {
  // The operator confirmed, then the tab was hidden. The other operator confirmed meanwhile, the
  // peer was stored, and the completed pairing left the server's list after its two minutes. The
  // first read after that lists the new peer and no pairing: the page must not say nothing was
  // stored (review 8238).
  const fake = await open(page);
  await page.getByLabel("Invite").fill(INVITE);
  await page.getByRole("button", { name: "Pair", exact: true }).click();
  fake.reachCode();
  const card = page.getByTestId("peers-pairing");
  await card.getByRole("button", { name: "They match" }).click();
  await expect(card.getByTestId("pairing-waiting")).toBeVisible();

  fake.finish();
  fake.state.pairings = []; // swept: the page never read the `done` state
  await expect(card.getByTestId("pairing-settled")).toHaveText(new RegExp(`${GONE_STORED}$`));
  await expect(page.getByText("Nothing was stored")).toHaveCount(0);
  await expect(page.getByTestId("peer-row")).toHaveCount(4);
  await expect(row(page, NEW_PEER)).toBeVisible();
});

test("a restart drops an open invite: the card says so and offers a new start", async ({ page }) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Create invite" }).click();
  await expect(page.getByTestId("invite-string")).toBeVisible();

  fake.state.invites = [];
  const ended = page.getByTestId("peers-invite-ended");
  await expect(ended).toContainText(INVITE_GONE);
  await expect(ended).not.toContainText("Nothing was stored");
  await expect(page.getByTestId("invite-string")).toHaveCount(0);
  await expect(page.getByTestId("invite-qr")).toHaveCount(0);

  await ended.getByRole("button", { name: "Start again" }).click();
  await expect(ended).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create invite" })).toBeEnabled();
});

test("the poll: 15 s at rest, 2 s while an invite is open", async ({ page }) => {
  await page.clock.install();
  const fake = await open(page);
  expect(fake.reads).toBe(1);
  await page.clock.fastForward(14_000);
  expect(fake.reads).toBe(1);
  await page.clock.fastForward(1_500);
  await expect.poll(() => fake.reads).toBe(2);

  await page.getByRole("button", { name: "Create invite" }).click();
  await expect(page.getByTestId("invite-string")).toBeVisible();
  const open0 = fake.reads;
  for (let i = 1; i <= 3; i++) {
    await page.clock.fastForward(2_100);
    await expect.poll(() => fake.reads).toBe(open0 + i);
  }
});

test("the poll stops while the page is hidden, resumes on return, and ends on leaving", async ({
  page,
  isMobile,
}) => {
  const fake = await open(page);
  await page.getByRole("button", { name: "Create invite" }).click();
  await expect(page.getByTestId("invite-string")).toBeVisible();
  // It is polling fast: two more reads arrive.
  const start = fake.reads;
  await expect.poll(() => fake.reads, { timeout: 10_000 }).toBeGreaterThanOrEqual(start + 2);

  const setHidden = (hidden: boolean) =>
    page.evaluate((h) => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => h });
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => (h ? "hidden" : "visible"),
      });
      document.dispatchEvent(new Event("visibilitychange"));
    }, hidden);

  await setHidden(true);
  // Let a read that was already on its way land, then watch three poll periods pass in silence.
  await page.waitForTimeout(500);
  const hidden = fake.reads;
  await page.waitForTimeout(6_500);
  expect(fake.reads).toBe(hidden);

  await setHidden(false);
  await expect.poll(() => fake.reads, { timeout: 1_500 }).toBeGreaterThan(hidden); // at once
  const back = fake.reads;
  await expect.poll(() => fake.reads, { timeout: 10_000 }).toBeGreaterThanOrEqual(back + 2);

  // Leaving the page ends it.
  if (isMobile) await page.getByRole("link", { name: "Back to settings" }).click();
  else
    await page
      .getByRole("navigation", { name: "Settings", exact: true })
      .getByRole("link", { name: "About", exact: true })
      .click();
  await expect(page.getByTestId("peers-settings")).toHaveCount(0);
  await page.waitForTimeout(500);
  const left = fake.reads;
  await page.waitForTimeout(5_000);
  expect(fake.reads).toBe(left);
});
