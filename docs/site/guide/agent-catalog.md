# The public agent catalog

The catalog is a list of **agent definitions and tested installation recipes**. It tells BattleLab
what an agent can do, where its exact release comes from, and which hashes must match. Agents
remain their own category. Playbooks describe workflows, skills supply reusable instructions,
and plugins are a separate kind of extension.

BattleLab ships with a usable catalog. **Settings → Agents** shows its source and links to the
[public definitions](https://github.com/teriansilva/agent-sessions/tree/main/release/recipes/linux-x64),
[change history](https://github.com/teriansilva/agent-sessions/commits/main/release/recipes/linux-x64),
and [signed releases](https://github.com/teriansilva/agent-sessions/releases). There is no required
catalog account or private admin service.

## What updates automatically

BattleLab checks public GitHub for signed catalog metadata once a day, and **Refresh catalog**
checks now. The last attempt and last successful check are shown separately. There is no switch
to turn the check off: it only downloads and verifies the catalog.

**Installed agents update themselves.** When the catalog (signed, and current) offers a newer
version of an agent you installed from it, BattleLab installs that version beside the current
one, runs its offline checks, and switches to it:
- **Offline checks only.** It checks that the program is the exact one the catalog pinned, that it
  reports its version, and that its store is there. It never starts a conversation with the
  vendor, so it never spends your quota or leaves history on your account. The agent row says
  *Updated automatically to X · offline checks*.
- **Running sessions keep their version.** Only new sessions use the new one, and the previous
  version stays installed: **Roll back** returns to it, and BattleLab never installs that rolled
  back version on its own again. A newer one still updates.
- **A version that fails its checks** is never retried. You stay on your version, the bell tells
  you once, and a newer version is tried when the catalog has one.
- **What never updates on its own:** an agent you set up from a local manifest or your own program
  file, an API agent, a disabled agent, and anything while the catalog has expired. A first install
  always asks you.
- **Pause** in Settings → Updates stops it (together with BattleLab's own updates). To give an
  automatically updated version the full check, disable it, choose **Verify**, then enable it.

## Updating an agent

When the catalog pins a different recipe than the one you run, and its version is not older than
yours, Settings → Agents shows *installed → available* (for example *2.1.286 → 2.1.311*) with
**Update**. A catalog version older than the installed one is never offered as an update; to go
back, use **Roll back** or enable a kept version. Update shows one
confirmation: the versions, the source, what verification does, and that your current version
stays active if it fails. Confirming installs the new version beside the current one, verifies it
with this host's existing vendor sign-in (no new sign-in unless verification fails, in which case
the sign-in step is offered), and then enables it. Running sessions keep the version they were
started with until they restart.

After an update, the previous version stays installed. **Roll back to …** on the agent's row
enables it again, after one confirmation, through the same enable step.

Offline, the bundled definitions are still available. Downloading an agent's artifacts needs
network access. Once a signed remote catalog has been accepted, it decides which agents are
offered: an agent it no longer lists is not offered from the bundle either. For an agent both
list, the newer version wins: when the installed BattleLab release bundles a strictly higher
version than the signed catalog pins, the bundled definition is offered (its source reads
*bundled with BattleLab*), whether the signed catalog is current or expired. Otherwise the signed
entry is offered, and once it expires it stays installable only if this release bundles the
exact same recipe. Unverified updates are rejected. Missing
or damaged saved trust records are reported and must be restored before catalog installation.

## Where the API agents fit

The native API agents are included with BattleLab. Each definition names the source command-line
agent whose installation and login it uses. They offer structured sessions alongside console
sessions. Set up the source agent and check readiness under **Settings → Agents**; there is no
separate API-agent executable to download. The source must meet the API adapter's required version.

## Edit a definition or add an agent

1. Fork the public repository and create a branch. Read the [manifest reference](./plugins).
2. Edit `release/recipes/linux-x64/<agent>.json` for a catalog recipe. It contains a manifest and
   the complete list of immutable artifact URLs, SHA-256 hashes and destinations. First-party
   behavior lives in `src/agent_sessions/plugins/first_party/<agent>/plugin.toml`; keep matching
   behavior consistent. Adding a new protocol or launch shape also needs a reviewed adapter
   implementation: definitions can only select capabilities BattleLab already understands.
3. Verify the upstream release and all dependency hashes, install in an isolated home, and record
   the actual required version/conversation/resume/usage checks. Follow the repository's
   `docs/first-party-plugin-release.md` evidence procedure. A new version number alone is not
   proof that the agent works.
4. Run `.venv/bin/python scripts/build-agent-catalog`, then
   `.venv/bin/python scripts/build-agent-catalog --check`. Commit the definitions, generated
   `src/agent_sessions/agent_catalog.json`, and test evidence together. Open a pull request.

The generated file is packaged into the next BattleLab release. It has release authority and
does not pretend to be a newly signed remote catalog. The builder and automated tests detect
when definitions change without rebuilding it.

## Publish an update independently of installation

Maintainers build a canonical remote cut from reviewed recipes using `scripts/build-plugin-feed`,
with a strictly higher sequence. The signing key stays outside the app, catalog server and CI. An
operator unlocks the primary key for the reviewed cut, signs it and verifies it against the
installed trust root; a web server cannot introduce its own signing key.

A cut is valid for up to seven days on its own. It can instead be approved for up to 90 days
(`--valid-for`): BattleLab then uses it only while the publisher's daily **freshness statement**
says it is still current. That statement comes from a separate, low-authority key that can't
approve or change anything, so the release key is needed only when the catalog changes, or at
the latest once per approval window.

**Publisher alerts.** The install that publishes the agent catalog and the gallery has
`AGENT_SESSIONS_FEED_PUBLISHER=1` in its env file (the publisher's deploy writes it; there is no
switch in the app). BattleLab then warns that install, in the bell and by push, when:
- a long-lived cut ends within 14 days, or a 7-day cut within 2 days;
- a cut has ended;
- a long-lived cut's freshness statement expires within 48 hours, or none is current.

The publisher's install checks both feeds every 6 hours, so a stale local copy is never reported as a stalled publisher. Each condition is announced once; once it clears, the next occurrence is announced again. Every other install hears only what matters to it, and only when it happens: *Agent updates are paused: the catalog's signature has expired. They resume on their own once it's renewed.* (An install that had the old in-app setting turned on keeps it until the env key is written, then the setting is removed.)

Publish the signed pair on the current public BattleLab GitHub release. Keep a versioned copy
of each pair (sequence and digest in the asset name), record its source commit, digest and expiry
in the release notes, then update the compatibility assets `plugin-feed.json` and
`plugin-feed.json.sig`. Clients check those names on the latest stable application release;
a partial publication is refused until both bytes match. Preserve the old versioned pairs for
audit and retain the relay's existing cuts for older clients. The next application release must
carry the current signed pair too. A catalog-only update does not require users to reinstall
BattleLab, but it does require the same public review and signing process.

Catalog checks are automatic. Newer upstream versions are proposed daily, after a 72-hour
cool-down, as one pull request to the bundled catalog; it merges on its own only after an
automated review approves its exact head and every check passes, and only when it changes
nothing but recipes and the bundled catalog. Renewing the signed metadata is maintainer work. The UI reports expiry and failed checks instead of claiming that the catalog
is always current. See `docs/release-signing.md` for the signing procedure.
