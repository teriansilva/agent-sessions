# Review every pull request

You can have an agent review every pull request on a repository. It reads the change, runs the
tests that cover it, and gives its verdict, the way a human reviewer would. In BattleLab this is no
special feature. It is a [webhook](/guide/webhooks), an [automation](/guide/automations) that
starts a session, and a [specialist](/guide/specialists) that knows how to review. So it works
with Forgejo, Gitea, GitHub, or anything else that can send a webhook.

## What you set up

| Piece | What it is | Where |
|---|---|---|
| A reviewer specialist | the reviewing agent: its skills (how to review), its agent, model and effort | Library → Specialists |
| A prompt template | the first message: "Review {{repo}}#{{pr_number}} at {{head_sha}}" | Library → Templates |
| A webhook | the address your forge sends pull request events to | Library → Webhooks |
| An automation | ties them together: which events, which values, which specialist | Automations |

## 1. The reviewer specialist

Make a specialist on an **API agent**. Give it a skill that says how you want reviews done: what
to check, how to treat the pull request's own text (as untrusted input), when to approve, and
where the verdict goes (posted on the pull request, or only kept in its session while you try it
out). If it should post reviews, give its project a forge token as a
[secret](/guide/secrets). The session reads it from there; the token never appears in a message.

## 2. The template

Make a template whose fields the delivery will fill, for example:

```text
Review pull request {{repo}}#{{pr_number}} at head {{head_sha}}.
```

Fields filled from a delivery must be plain: a number, a short text (up to 200 characters, no line
breaks) or yes/no. The pull request's title and body are never passed. The reviewer reads the
pull request itself.

## 3. The webhook

Create a webhook and add its address and secret to the repository's webhook settings, with the
pull request events. Send one test delivery, so the wizard can offer its fields.

## 4. The automation

**Automations → New**, then:

1. **When:** a webhook delivery, from the webhook above.
2. **Events:**
   - pick the preset **PR opened or pushed (drafts skipped)**;
   - add **repository.full_name is** your repository, so a webhook shared with other repositories
     doesn't start a review here;
   - add **sender.login is not** your reviewer's account, so its own activity starts nothing;
   - under **Run once per**, choose `repository.full_name`, `pull_request.number` and
     `pull_request.head.sha`. Each pushed revision is then reviewed once, even if your CI re-sends
     the same event with a slightly different body.
3. **What:** **Start a session**:
   - pick the reviewer's agent and a folder to work in;
   - under **Run as a specialist**, pick your reviewer;
   - for the first message, pick the template and map its fields: `repo` ←
     `repository.full_name`, `pr_number` ← `pull_request.number`, `head_sha` ←
     `pull_request.head.sha`.
4. **Limits:** keep the daily cap. It bounds how many reviews a busy day can start. "One at a
   time" (the default) skips a delivery while a review is still starting; a skipped delivery
   claims nothing, so the next one for that revision still runs.
5. **Review:** read the approval lines and approve. They name the specialist and its version,
   and the values that make a run unique.

## What happens on a push

1. The forge (or your CI, once its checks pass) sends the pull request event to the webhook.
2. The automation checks the conditions. Drafts, other repositories and the reviewer's own pushes
   start nothing.
3. If a review already ran for this revision, nothing starts. Otherwise one session starts as your
   reviewer, with the template's message filled in.
4. The reviewer reads the pull request, checks it is still open at that revision, reviews it, and
   gives its verdict.
5. The author pushes a fix. That is a new revision, so the next delivery starts the next review.

Fix rounds are the author's job, as with a human reviewer. The automation reviews each revision
once.

## Good to know

- **Editing the specialist pauses the automation** until you approve again: a new version may
  review differently, so you see it before it runs. That holds right up to the launch: a review
  already starting when you edit it doesn't start.
- **A revision is reviewed at most once.** If a review session fails, the same revision isn't
  retried on its own: you get a notification, and the next push starts a new review.
- **Skipped deliveries can run later.** If the automation was busy (one at a time) or at its daily
  cap, that delivery claimed nothing, so a later delivery for the same revision still runs.
- **A delivery missing one of the "once per" values is refused** and shows in the run history.
- **Unattended reviews:** under *Model, permissions and closing*, tick:
  - **Permission bypass**, so the reviewer doesn't stop at its first tool call;
  - **Close the session when it's done**, so each finished review is archived. This frees the
    specialist for the next review and keeps your session list clean.

  Both are part of what you approve.
