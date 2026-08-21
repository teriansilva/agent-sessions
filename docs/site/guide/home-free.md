# Home Free

Home Free reaches your box from any browser **without** port-forwarding, dynamic DNS, or a VPN —
by streaming it through a relay that cannot read what it forwards.

It is opt-in. Self-hosting behind your own TLS and reverse proxy remains the default, and Home Free
changes the security model in ways worth understanding before you enable it.

## How it works

The installer generates two things into `~/.local/share/agent-sessions/homefree/`, mode `0600`:

- a **console name** — a random callsign like `viper-8231`;
- an **access key** — machine-generated, at least 128 bits, never user-chosen and never sent
  anywhere.

It then installs a second user unit, `agent-sessions-homefree.service`. At runtime that agent holds
a single **outbound-only** WebSocket to the relay, registers the console name under a long-term
Ed25519 identity, and redials with exponential backoff when the link drops. **No inbound port is
ever opened.**

To connect you open the public viewer page, enter the console name and access key, and your browser
and the agent run an end-to-end encrypted handshake *through* the relay — X25519 ephemeral keys and
HKDF-SHA256 with the access key as a pre-shared key, then AES-256-GCM transport with
strictly-increasing counters.

## What the relay can see

Nothing. It forwards opaque binary frames. The access key, your session cookie, terminal traffic
and file contents never exist in plaintext outside your machine and your browser — asserted by a
regression test that searches relayed frames for plaintext and finds ciphertext only.

The design also gives you forward secrecy (ephemeral keys per session) and replay resistance
(out-of-order and replayed frames are dropped).

The tunnel is **not generic**: on the agent side every stream is reverse-proxied to one fixed
target, the box's own loopback app. No path or header a viewer sends can redirect it at other
loopback services.

## What enabling it changes

Three things, and they are the reason this is opt-in:

1. **The app must stay loopback-bound.** Stream mode refuses any other bind.
2. **Local auth is switched off** (`AGENT_SESSIONS_AUTH_MODE=none`) so the **access key becomes the
   single gate**. CSRF and Origin checks stay on.
3. **That key grants full control of the box.** It is the only gate, and BattleLab launches agents
   with permission bypass. Treat it exactly like an SSH private key.

::: warning The access key is the whole security boundary
Anyone with the console name and access key has what an SSH session would give them. Rotate it if
it is ever shared, pasted somewhere it should not be, or typed on a machine you do not trust.
:::

## Running your own relay

Nothing here depends on the public instance. `AGENT_SESSIONS_RELAY_URL` and
`AGENT_SESSIONS_CONNECT_URL` point the agent and the viewer page at a relay you run yourself; the
protocol is unchanged.

## Outbound connections

Everything the self-hosted mode makes, plus the one persistent WSS connection to the relay.

::: info Verified against
Commit `218cf3a` — `docs/infrastructure.md § Mode 2 — stream via BattleLab (Home Free)`; `docs/home-free-handshake.md`; `src/agent_sessions/homefree/`.
:::
