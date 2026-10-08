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

BattleLab checks public GitHub for signed catalog metadata once a day. You can turn this off
with **Automatically check daily**, or use **Refresh catalog** yourself. The last attempt and
last successful check are shown separately. Refreshing the catalog does not install software,
change an enabled agent, or interrupt sessions. **Review update** starts a separate installation
review; sign-in, verification and enabling remain explicit steps.

Offline, the bundled definitions are still available. Downloading an agent's artifacts needs
network access. Once a signed remote catalog has been accepted, its entries constrain the
bundled fallback: if it expires, only unchanged recipes that it still offers and that also ship
with the installed BattleLab release remain eligible. Unverified updates are rejected. Missing
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
with a strictly higher sequence and a validity interval of at most seven days. The signing key
stays outside the app, catalog server and CI. An operator unlocks the primary key for the reviewed
cut, signs it and verifies it against the installed trust root; a web server cannot introduce
its own signing key.

Publish the signed pair on the current public BattleLab GitHub release. Keep a versioned copy
of each pair (sequence and digest in the asset name), record its source commit, digest and expiry
in the release notes, then update the compatibility assets `plugin-feed.json` and
`plugin-feed.json.sig`. Clients check those names on the latest stable application release;
a partial publication is refused until both bytes match. Preserve the old versioned pairs for
audit and retain the relay's existing cuts for older clients. The next application release must
carry the current signed pair too. A catalog-only update does not require users to reinstall
BattleLab, but it does require the same public review and signing process.

Catalog checks are automatic; approving upstream versions and renewing the signed metadata are
maintainer work. The UI reports expiry and failed checks instead of claiming that the catalog
is always current. See `docs/release-signing.md` for the signing procedure.
