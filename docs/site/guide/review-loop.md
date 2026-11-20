# Automatic review loop with Forgejo

A **review loop** reviews every issue and pull request in one Forgejo repository without you
starting anything:

1. An issue is opened or edited, or a pull request is opened or pushed.
2. BattleLab starts your **reviewer specialist** on exactly that revision.
3. The reviewer posts its verdict on Forgejo, with its own account.
4. BattleLab sends the verdict to the **API session that opened the issue or pull request**. That
   session fixes it and pushes (or edits the issue), which starts the next review.
5. The loop ends when the reviewer approves, or when the item has had its maximum number of reviews.
   At that point you get one notification.

BattleLab never posts, merges or labels anything on the forge. The reviewer writes with its own
credentials and the working session pushes with its own.

## Before you start

- **A reviewer specialist on an API client.** Create it in **Library → Specialists**. Give it the
  skills your reviews need, and a forge tool or credentials so it can post as the reviewer account
  (for example a bot user). Nothing unattended drives a terminal, so a terminal-only agent can't
  be the reviewer.
- **Working sessions on an API client too.** The verdict is delivered only into an API session.
  If the session that opened the item is a terminal session, nothing is typed into it: the run
  says so and you get a notification with a link to the session.
- **The forge in Settings → Forge.** Fill in its address and a token that can read the repository.
  Before every review, and again before every verdict is accepted, the loop reads the item's
  current revision there. A late or out-of-order delivery therefore never starts a stale review.
  If the forge can't be read, nothing happens.
- **The session record.** The loop finds the session that opened an item from its session record:
  - for a pull request, `<!-- agent-session-pr: engine=… id=… -->` in its description;
  - for an issue, `<!-- agent-session: … -->` or `<!-- agent-session-author: … -->` in a comment.

  Without a record, a pull request falls back to the one API session in the project whose
  checkout is on the pull request's branch.

## Set it up

1. Open **Library → Automations → New automation**.
2. Under **When**, choose **Forgejo webhook**. Under **Do**, **Review loop** is selected with it.
3. Pick the **project**, then enter the **repository** (`owner/name`), the **reviewer specialist**
   and the **reviewer account** (the forge login the reviewer posts as).
4. Choose what to review (issues, pull requests or both) and how many reviews an item gets at
   most. The defaults are 4 per pull request and 5 per issue, counted over the item's whole life.
5. Save, then **Enable…**. The consent dialog lists everything the loop may do; confirm with
   **Enable automation**. Any later change asks again.
6. In **Connect Forgejo**, choose **Create secret** and copy it. It is shown once.
7. In the repository on Forgejo, open **Settings → Webhooks → Add webhook → Forgejo** and set:
   - **Target URL:** the payload URL from the panel (`https://<your BattleLab>/hooks/a/<id>`).
   - **HTTP method:** POST. **Content type:** `application/json`.
   - **Secret:** the secret you copied.
   - **Trigger on:** custom events, and tick **Issues**, **Issue comment**, **Pull request**,
     **Pull request synchronized** and **Pull request reviewed**. "Pull request synchronized" is
     a separate box on Forgejo: without it, a push to an open pull request never starts the next
     review.
   - **Branch filter:** leave empty.
8. Use **Test delivery** on Forgejo. **Last delivery** in the panel updates. A test delivery isn't
   an item, so it isn't a run.

**Rotate secret** makes a new one. The old secret keeps working for 24 hours, so you can update the
forge without missing deliveries.

### Behind a reverse proxy

The webhook authenticates itself with its signature. If your proxy asks for a login in front of
BattleLab (for example an `auth_request`), let `/hooks/` through without it. Otherwise the forge's
deliveries never arrive and the panel keeps saying **No delivery yet**.

## What the reviewer is told

The reviewer's first message names the item, the revision, the review round, and one line it must
include in its verdict:

```
<!-- battlelab-review: record=<id> item=pr#41 rev=<head commit> verdict=approve|changes -->
<!-- battlelab-review: record=<id> item=issue#39 rev=<revision> verdict=viable|adjust|clarify -->
```

- **Pull request:** the verdict is a formal review: **Approve** for `approve`, **Request changes**
  for `changes`. The review type has to match the marker.
- **Issue:** the verdict is one comment.

A verdict without the marker isn't counted. Neither is a verdict for an older revision, or a
second verdict for the same review.

The reviewer is told to treat everything it reads on the forge as material to review, never as
instructions. The working session gets a short fixed message: who reviewed what, the verdict,
the round, and "read the review on the forge". The review text itself is never pasted into it.

## Run history

Every delivery about an item is a run. Each row shows its leg (**review** or **return**), the item
and revision, and for a review its count (*review k / N*), with a reason line that says what
happened:

| Shown as | Means |
|---|---|
| OK | **review:** the reviewer started on this revision. **return:** the verdict reached the session that opened the item, or, in a loop that returns no verdicts, it was recorded. An approval ends the loop for that revision. |
| Skipped | That revision was already reviewed (the forge sent the same event twice), or the daily limit was reached. A skipped delivery can be redelivered from Forgejo later. |
| Ignored | Stale (the forge has moved on, or the item is closed), for an unknown or superseded review, a duplicate verdict, or a marker that doesn't fit. |
| Capped | The item already had its maximum number of reviews. No review started; you're notified once. |
| Not delivered | The verdict couldn't reach a session: no session record, a terminal session, more than one match, or a busy session. Never resent; you're notified. |
| Refused | The delivery was for another repository or malformed, or the forge couldn't confirm the current revision. |
| Interrupted | BattleLab stopped while it was acting. Never repeated; for a verdict you're notified, so you can tell the session yourself. |

A delivery with a missing or wrong signature gets the same "not found" answer as an unknown
address. It is counted in the panel, never run. A human's comment or a label change only updates
**Last delivery**.

Reopening an item without new commits or a new title or body doesn't start another review: the
earlier verdict stands.

**Run now** isn't available for a review loop, because only a delivery names the item to review.
