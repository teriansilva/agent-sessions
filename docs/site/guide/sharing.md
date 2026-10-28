# Sharing skills and playbooks

Move skills and playbooks between BattleLab installs: your own second machine, or a colleague's.
You export a selection to **one file** (`*.battlelab`) and import that file on the other install
through a **preview**. Importing always makes **new copies** and never changes anything that was
already there.

## Export

1. Open **Library → Skills** or **Library → Playbooks** and choose **Export…**.
2. Tick what to share. Anything a chosen item needs comes along and is marked **Included**. A
   playbook's role skills are included, for example. An included item can't be unticked on its
   own; untick the item that needs it instead.
3. Choose **Download bundle**.

The file holds the chosen items exactly as written; no settings, deployment files or secret
variables are added. **Check the text before you share it:** a token, password or key pasted into a
skill or playbook file goes into the bundle with it. A role skill
your library doesn't hold travels as a name only, the same as it works on this install.

## Import

1. Choose **Import…** on either Library page, then choose or drop a `.battlelab` file (up to 32 MB).
2. BattleLab checks the whole file before it shows anything. A file that isn't a valid bundle is
   refused with the reason, and nothing is read into your library. A bundle made by a newer
   BattleLab asks you to update first.
3. The preview lists every item and what it becomes here:
   - **New copy**: the ID is free.
   - **Renamed**: an item with that ID already exists here. Yours stays untouched; the copy gets a
     new ID such as `tdd-2`. A playbook in the same file that uses a renamed skill is pointed at the
     new ID.
   - **Refused**: it can't be imported, with the reason. If you untick a skill that a playbook
     needs, the playbook is refused too ("needs skill …, which isn't being imported"). Nothing is
     ever created without what it needs.
4. Open **Review files** on an item to read its complete text. A file too large to show, or not
   text, can't be reviewed there: importing it needs an explicit acknowledgement. For a renamed
   playbook, or one that uses a renamed skill, the files are shown as sent. On import a renamed
   playbook's ID changes, the role skill names in its flows follow any renamed skill,
   and each rewritten TOML file is saved without its comments or original layout. Bundles aren't signed, and skills and
   playbooks are instructions your agents will read.
5. Choose **Import**. Nothing changes until you do.

The result lists each item as **Created** (with an **Open** link), **Refused**, **Failed** or
**Skipped** (it needed an item that failed). Imported copies carry an **Imported from a file**
mark. Nothing is activated, deployed, launched or made a default by an import.

### When to preview again

A preview lasts 15 minutes and matches your library as it was when the preview was made. If the
preview expired, or your library changed in the meantime (for example, a skill named `tdd-2` was
created), BattleLab asks you to **Preview again** instead of applying an out-of-date plan.

Confirming the same preview twice is safe. Items already imported are reported as they were, and
no second copy is made. **Retry failed items** tries only what didn't make it.

## Coming later

Specialists, templates and the variable library, and a project's memory notes will use the same
file. Secret values will stay out unless you choose to include them encrypted under a passphrase
you type.
