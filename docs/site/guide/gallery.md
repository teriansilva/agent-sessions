# Playbook and skill gallery

**Library → Gallery** shows public, versioned playbooks and skills. Agents remain in their own
catalog. A playbook describes a workflow; a skill contains instructions and supporting text files.

Choose **Check public gallery** to fetch the latest signed publication. You can also browse the
[public gallery](https://relay.battlelab.superstatus.io/catalogs/gallery) without an admin account.
The app contacts only `/catalogs/gallery/v1/current.json` and the descriptor-selected
`/catalogs/gallery/v1/releases/<sequence>-<digest>/gallery-index.json` and `.sig` on
`https://relay.battlelab.superstatus.io`. These reads send no account credentials or cookies.
The signed index embeds complete content; source links are never fetched automatically.

The public page exposes complete files, author, publisher, requirements, source, content digests,
signed publication and the catalog service's source offer. The protected admin is for authoring
and publishing.

## Import a copy

1. Open an entry and inspect its requirements and complete files. Playbooks also show validated
   inputs and workflow steps when this BattleLab version supports their format.
2. Choose a local copy ID and acknowledge that you reviewed the content.
3. Select **Import editable copy**, then **Open local copy**.

An imported playbook joins your local Playbooks library. Only its local playbook ID is rewritten;
flow and step identities are retained. You can edit it with the existing playbook editor, whose
ordinary **Save** still edits that local playbook in place with its usual revision checks.
The separate **Create updated copy** action described below leaves that original untouched.
An imported skill appears under **Library → Skills**, where you can edit its files or add supporting
text. Choose a free **New copy ID**, then **Save edited copy** to keep your changes separately.
**Open new copy** takes you to the result; the original skill stays unchanged. The edited copy keeps
the same imported version and source history, so its local edits can be compared with future updates.
Saving a skill here does not activate it in any agent. Importing never runs a workflow,
installs an agent, changes your default playbook or updates a deployed project.

The **Gallery source** panel records where the copy came from, its imported version and whether
you have changed it. Source and base evidence are retained separately from your editable files.

If someone changes the original skill's files on disk while you are editing, choose **Compare stored version**. Your
draft edits stay in place, while files you have not edited pick up the stored changes, including
new files. Review the comparison, then explicitly save an edited copy of that revision.

## Review an update

Select **Compare gallery updates** from the local copy, choose a published version, and review
three columns: **Imported base**, **Local copy**, and **Incoming**. Changes you have not edited
locally are proposed automatically. Conflicting files require an explicit choice or a manually
edited result. Choose a free **New copy ID**, acknowledge the results, then select
**Create updated copy**. **Open new copy** takes you to the result.

Your original copy, its edits and source history stay unchanged. Existing projects and the default
selection keep using the original. Choose the new copy explicitly when you are ready to use it.
The new copy records the selected upstream version as its base and retains the earlier source
history. For playbooks, only the local playbook ID is rewritten; workflow identities stay the same.

Copy creation checks the local revision and source you compared again before publication. If that
check detects an edit, your draft remains on screen; compare again and review before creating the
copy. A later edit on disk remains in the original; creating a copy never replaces that directory.
An occupied destination ID is refused, so choose a different ID without discarding your review.
Ordinary local edits never fetch or apply upstream updates. Previously deployed projects stay
pinned until you update them through their separate deployment flow.

## Expiry, failed requests and recovery

An expired publication remains inspectable, but new imports and updates require a fresh signed
publication. Withdrawn versions retain their full content for inspection. Existing local copies
remain available when the public service is offline. Saving local skill edits as a new copy works
without a fresh public publication; it uses the source history already stored on your device.

If an import, update or edited-copy response is lost, use **Check operation status**. Reloading does not repeat
the write. An uncertain result requires inspecting your local library and retained copies before
starting another attempt. A durability warning means the content was published but its flush
could not be confirmed; it does not mean the write was undone.

Recovery of an interrupted import or updated-copy creation requires both the new copy's source
marker and its exact planned content. If the files have since changed and no completed outcome
was recorded, it stays uncertain. Checking status never replaces local edits or creates another
copy. A skill without gallery source history cannot prove an interrupted copy's identity, so it
stays uncertain if completion was not recorded. A failed publication check leaves the destination in place for inspection. The original
copy is unaffected. Pending operations from older in-place update implementations remain uncertain
unless their completion was recorded.

Local files require a filesystem that supports user extended attributes. If source evidence is
missing or damaged, BattleLab shows that problem and refuses edits until the matching evidence
is restored. Keep `gallery-state`, the local playbooks directory and `skills-library` together in
backups. Do not delete source records or reset publication floors to bypass an error.

## Add or publish gallery content

Authors use the protected publisher's **Create draft → New playbook / New skill** actions, edit
metadata and files, save a validated draft, and review its preview. Published versions are
immutable: changes require a new version. Publishing requires an external signature from an
installed trusted release key and explicit confirmation of the exact proposed publication.
The publisher's source offer includes its `GALLERY.md` guide and content contract.
