# Webhooks

A **webhook** lets another service tell BattleLab that something happened: a pull request was
opened, a deploy failed, a form was sent. In BattleLab a webhook is just three things:

- a **name** you choose;
- an **address** the service sends to;
- a **secret** the service signs its deliveries with.

It doesn't care who sends to it. What a delivery *does* is up to the
[automations](/guide/missions) that use the webhook. One webhook can serve several automations,
each with its own approval.

## Create one

1. Open **Library → Webhooks → New webhook** and give it a name, e.g. "shop repo".
2. BattleLab shows the **address** and the **secret**. The secret is shown **once**: copy it now.
3. In the other service's webhook settings:
   - **URL:** the address.
   - **Content type:** `application/json`.
   - **Secret:** the secret.
4. Send a test delivery from the service. **Last delivery** on the webhook's page updates.

## Which services work

Any service that signs its deliveries with HMAC-SHA256 of the request body and sends the
signature in one of these headers:

| Header | Sent by |
|---|---|
| `X-Forgejo-Signature` | Forgejo |
| `X-Gitea-Signature` | Gitea |
| `X-Hub-Signature-256: sha256=…` | GitHub and many others |

There's no provider to choose. A delivery with a missing or wrong signature gets the same "not
found" answer as an unknown address, and nothing runs. The webhook page counts these as
"unsigned deliveries in 24 h", so you notice a misconfigured sender.

## What BattleLab keeps

For the last delivery, BattleLab keeps **the names and types of its fields**, for example
`pull_request.number · number` or `action · text`. When you set up an automation, you pick from
these names. **The values themselves are not kept.**

## Choose which events, and fill values from them

A webhook can start a review loop, start a session, or send a message to a session. Sessions must
be API sessions.

- **Which events.** If the webhook has received an event, BattleLab offers presets for common
  ones: *PR opened or pushed (drafts skipped)*, *Issue opened or edited*, *Review submitted*. You
  can also add your own conditions on the delivery's fields (*is*, *is not*, *is one of*,
  *contains*, *is yes / no*, *is present / absent*). Conditions read the signed body only, never
  a header.
- **Fill a template from the delivery.** Each template field can take a value from a field of the
  delivery, with a type: a number, a choice from a list you give, a short plain text (up to 200
  characters), or yes / no. If a delivery's value doesn't fit its type, nothing starts, and the
  run history says which field failed.
- **The same delivery never runs twice**, even when it is delivered again.

## Rotate, rename, delete

- **Rotate secret** makes a new secret, shown once. The old one keeps working for 24 hours, so you
  can update the service without missing deliveries.
- **Rename** changes only the name. The address stays the same.
- **Delete** works only while no automation uses the webhook. Change or delete those automations
  first.

## On a Home Free box: through the BattleLab relay

A Home Free box has no public address of its own. Its webhooks get one on the BattleLab relay
instead, like `https://<id>.hook.battlelabos.com/`. That's the address the webhook page shows. The
relay passes each delivery straight on to your box, where the signature is checked as usual.

- The relay **sees what a delivery contains while passing it on** (unlike your sessions, which are
  end-to-end encrypted). It **doesn't store it** and has **no secret**, so it can't fake a delivery.
- If your box is offline, the sender gets an error and can deliver again later. Forgejo has
  **Redeliver** for this.
- Deleting a webhook removes its relay address first. If the relay can't confirm that, the webhook
  stays, and you can try again.
- After the box or its relay connection restarts, the relay addresses work again within about a
  minute. They keep the same address.

## Behind a reverse proxy

The webhook authenticates itself with its signature. If a proxy in front of BattleLab asks for a
login (for example an `auth_request`), let `/hooks/` through without it.

## Review loops from before webhooks were a list

A review loop set up before this change keeps its own address (`/hooks/a/…`) and secret. It
appears in the list as "old address", and only that review loop uses it. Nothing about it changes
until you move it over.
