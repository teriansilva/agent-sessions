# Playbook and skill gallery

The gallery is a public collection of ready-made playbooks and skills. You don't open it from its
own menu entry. Each kind has a **Browse gallery** view next to your own library:

- **Library → Playbooks** has **Your playbooks** (the ones new projects can use) and **Browse
  gallery** (public playbooks you can add).
- **Library → Skills** has **Your skills** and **Browse gallery** (public skills).

A playbook describes how a repository works: its workflow, inputs and files. A skill is a set of
instructions with supporting text files. Agents have their own catalog under Settings.

Opening a Browse gallery view, or a copy that came from the gallery, checks the public gallery once
per page load, so you never need to press a refresh button. **Refresh** is there if you want to check again. You can also browse the
[public gallery](https://relay.battlelab.superstatus.io/catalogs/gallery) without signing in.

## Add a playbook or skill

1. Choose **View** on a card. The page shows, in order:
   - what it is and who made it;
   - its **Requirements**;
   - **What's inside** (inputs and workflow steps, for playbooks);
   - its **Files**. The first file is open, and every other file shows its complete content with
     **Show file**, or all at once with **Expand all**. A long file scrolls inside its own viewer.
2. When you have checked the requirements and files, choose **Add to my playbooks** (or **Add to
   my skills**). On a phone the button sits after the files; on a wide screen it stays beside them.
3. Confirm with **I've reviewed it — Add**. The confirmation is your statement that you checked
   it. Nothing is added before you confirm.
4. Choose **Open** to go to your new copy.

Your copy is ordinary editable files in your library. Adding never runs a workflow, starts an
agent, changes your default playbook or touches a project. A skill you add is not turned on in any
agent.

The copy gets the entry's own name (for example `inbox-triage`), or the next free one
(`inbox-triage-2`) if you already have that name. If the name gets taken between your click and
the save, the confirmation stays open, says which name it will use instead, and waits for you to
confirm again. Nothing is ever retried on its own. To pick a different name yourself, open
**Technical details** on the entry and change **Local ID**.

## Keep a copy up to date

A copy that came from the gallery shows one line on its page, such as *From the gallery · v1.2.0 ·
edited locally*. When the gallery has a newer version of the same entry, it also shows **Update
available** and a **Review update** button.

**Review update** shows the version change (for example *v1.2.0 → v1.3.0*):

- If none of the changed files conflict with your own edits, it says so in one line and you can
  choose **Add updated copy** straight away. The changed files are listed under one collapsed line.
- If a file conflicts, only that file shows **Your edit**, **Gallery version** and **Original** side
  by side. Choose one, or type the result you want. **Add updated copy** waits until every
  conflicting file has a result.

The update is always added as a **new** copy (for example `inbox-triage-v1-3-0`). Your current copy,
its edits, the projects using it and your default playbook stay exactly as they are. Use the new
copy when you are ready.

If the copy changed on disk while you were reviewing, adding refuses and keeps your choices on
screen. Choose **Compare again**, check the new state, then add.

Instead of **Update available**, a copy can show one of these:

| You see | It means |
|---|---|
| *Can't check for updates right now.* | The public gallery hasn't been reached yet. |
| *The gallery listing is out of date…* | The last listing expired. Updates wait until it refreshes. |
| *This has been withdrawn from the gallery.* | The entry was withdrawn. Your copy keeps working. |
| *Its gallery history can't be read…* | Its saved gallery history is damaged. Changes wait until it is restored. |

## When something goes wrong

| You see | What to do |
|---|---|
| *The public gallery can't be reached right now. Your playbooks and skills are not affected.* | Nothing is broken on your side. Choose **Try again** later. If the gallery was checked before, the last list stays visible. |
| *Nothing has been published to the gallery yet.* | There is nothing to add yet. |
| *This gallery listing is out of date, so adding is paused until it refreshes.* | Choose **Refresh**. You can still look at everything. |
| *The public gallery sent a listing that couldn't be verified, so nothing changed.* | The gallery's signature didn't check out, so BattleLab kept what it had. Try again later. |
| *The gallery listing saved on this device can't be read.* | Restore the gallery state from a backup (see below). |
| *We couldn't confirm whether adding … finished.* | The answer to an add or update was lost. Choose **Check status**. It tells you whether it was added. Adding stays paused until you check, and reloading never repeats it. |

An old link to the former **Library → Gallery** page still works. It opens the matching view with a
note that the gallery now lives in Playbooks and Skills.

## Technical details

Every entry, listing and copy has a collapsed **Technical details** section. It holds the content
digests, the signed publication's sequence and expiry, the source link, the imported base of a copy
and the ID of a pending operation. You don't need any of it day to day.

How it stays safe:

- BattleLab only reads `/catalogs/gallery/v1/current.json` and the matching
  `/catalogs/gallery/v1/releases/<sequence>-<digest>/gallery-index.json` and `.sig` on
  `https://relay.battlelab.superstatus.io`. These reads send no account credentials or cookies.
  Source links are never fetched.
- The listing must be signed by the installed gallery key and contains every file in full. Every
  file is checked against its signed digest before you can view or add it.
- Published versions never change. Withdrawn versions stay viewable but can't be added.
- An expired listing stays viewable, but adding and updating wait for a fresh one.

Local files need a filesystem with user extended attributes. Keep `gallery-state`, the local
playbooks directory and `skills-library` together in backups, and don't delete source records or
reset publication floors to work around an error.

## Add or publish gallery content

Authors use the protected publisher's **Create draft → New playbook / New skill** actions, edit
metadata and files, save a validated draft, and review its preview. Published versions are
immutable: changes require a new version. Publishing requires an external signature from an
installed trusted release key and explicit confirmation of the exact proposed publication.
The publisher's source offer includes its `GALLERY.md` guide and content contract.
