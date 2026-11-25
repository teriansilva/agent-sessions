# Artifact repositories

An **artifact repository** is a named collection of artifacts that this BattleLab hosts: design
assets, test reports, release builds, anything you want to keep together under one name. Every
item in it is a frozen **revision**, exactly like an artifact's, so a repository never changes
under you: adding a new version adds a new revision.

Open **Library → Artifacts → Repositories**. The tab beside **My artifacts** lists the
repositories hosted on this BattleLab, each with its number of items and the storage it uses.

::: info Coming later
Giving a paired BattleLab access to a repository (to read it, or to add to it) is a later step.
Today a repository lives on this BattleLab only, and nothing in it leaves this machine.
:::

## Create a repository

Select **New repository**, give it a **name** and, if you like, a one-line description. A name is
1–64 lowercase letters, digits, dots, dashes or underscores (`design-assets`, `release-builds`).
Agents use the name to publish into the repository, so keep it short.

Every repository holds up to **2 GB**. The storage bar on its page shows how much it uses.

## Add an artifact

On a repository's page, select **Add an artifact…**, pick one of your artifacts and select
**Add to repository**. Its latest revision becomes a new item. Under **Add as** you can instead
make it a new revision of an item that is already there (an item keeps its kind: a site stays a
site, a file stays a file).

Adding copies **no bytes**. The repository keeps its own record of the revision, so the item stays
in the repository even after you delete the artifact it came from.

## Let an agent publish into a repository

An agent that has the artifacts tool can publish straight into a repository on this BattleLab.
Ask it in your own words, for example: “Publish the load-test report as an artifact into the
`test-reports` repository.” The agent passes the repository's name to `artifact_publish`.

- The publish goes through **the same checks** as any artifact: the session's own folders only, no
  hidden files, no key files, no archives, and a file with a secret in it refuses the whole publish.
- It makes the artifact (with its usual private link) **and** the repository item, together. If
  the repository is full or doesn't exist, nothing is published, and the agent hears why.
- Publishing the same artifact again into the same repository adds a **new revision** of its item.

## Open and remove items

Each item shows its kind (SITE, PNG, PDF…), who added it (**you**, or the agent session), its size
and its revision.

- **Open** shows the item's latest revision in a new tab, served the same way as your own
  artifacts: sandboxed, and only for you, signed in.
- **Remove** takes the item and every revision of it out of the repository, after you confirm.
  Your own artifact, if it still exists, is not touched.
- **Delete repository…** removes the repository and every item in it.

Space comes back as soon as nothing else uses the same files.

## Storage

- A repository counts each file it holds **once**, however many items or revisions name it.
- Repository files count toward the store's total (**Settings → System → Artifacts**) along with
  your artifacts, again once per file, whoever holds it.
- When adding something would put a repository over its 2 GB, nothing is added and you see why.
