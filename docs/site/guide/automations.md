# Automations

An **automation** does one thing for you without you starting it: on a schedule, when you press
**Run now**, or when a [webhook](/guide/webhooks) delivers an event. It can start a session, send a
message to a session, tell HQ, or (on a schedule or Run now) start a mission.

You find them in **Library → Automations**. Every automation is created and changed in the same
**wizard**: five short steps, each one screen on a phone. Advanced options are folded away until
you open them.

## The five steps

### 1. When

Choose when it runs:

- **On a schedule.** Once at a set time, every N minutes or hours, daily, weekly or monthly, in a
  time zone you choose. Every 5 minutes is the shortest schedule. Runs missed while BattleLab was
  down are collapsed into one catch-up run.
- **When a webhook delivers.** Pick a webhook from **Library → Webhooks**, or choose
  **+ New webhook…** to make one. You come back to the wizard with your answers kept and the new
  webhook picked. The webhook's address and last delivery are shown below the list.
- **Only when I press Run now.** Nothing runs on its own.

### 2. Which events

Only for a webhook. A schedule and Run now have no events, so the wizard skips this step and says
so.

- **Presets.** Once the webhook has received a delivery, BattleLab recognises common events from
  its fields and offers them, for example *PR opened or pushed (drafts skipped)*, *Issue opened or
  edited* or *Review submitted*. Choosing one copies its conditions into the automation, so a later
  BattleLab update never changes what an enabled automation acts on.
- **Every signed delivery.** With no conditions, every delivery starts it.
- **Advanced: your own conditions.** Pick a field of the last delivery (or type one, like
  `pull_request.draft` or `labels[].name`), an operator and a value. The operators are *is*,
  *is not*, *is one of*, *contains*, *is yes*, *is no*, *is present* and *is absent*. All
  conditions must hold; there are at most 8, and nothing else (no expressions). *Is one of* takes
  one value per row (**Add a value**), so a value may contain a comma. Each value is read as its
  own type, the field's by default: `4` for a number field, *yes* or *no* for a yes/no field.

If the webhook has had **no delivery yet**, BattleLab can't offer presets. Send a test delivery
from your service and choose **Check again**, or set your own conditions under Advanced. A sender
BattleLab doesn't recognise gets Advanced only.

Conditions read the delivery's signed body, never its headers. A field that wasn't in the last
delivery is flagged in amber: the condition may never match.

### 3. What

Choose what it does from a short, searchable list: the same actions **Library → Actions** shows
(see [Actions](/guide/actions) for what each effect class means). The list holds only what the
trigger from step 1 can start:

- **Start a session**: an agent, a folder and a first message. The model is the agent's default.
  **Permission bypass** and **Close the session when it's done** are under *Model, permissions and
  closing*. Turning either on asks for your consent again.
- **Message a session**: types into a session that is already running. If it isn't running then,
  the run is refused and recorded.
- **Start a mission**: only on a schedule or Run now. HQ drafts the mission in the project you
  choose.
- **Tell HQ**: puts a message in your notifications (the bell), from any trigger. HQ sees that a
  message from this automation arrived, and may write its usual line about it. HQ never sees what
  the message says or what a delivery contained, and it never acts on the message by itself. The
  bell shows the message on one line (a long one is cut short); the run history keeps all of it.
  A template with a secret can't be used here, because the message is shown in your
  notifications.

The wizard offers **API agents** for new automations: *Start a session* lists the API agents that
may work unattended, and *Message a session* lists API sessions. An installed API agent that isn't
cleared for unattended work yet is named under *Why aren't other agents listed?*. The server's rules
are unchanged, and so are existing automations on a terminal agent or session: they keep their
target, labelled, and still save.

The message is plain text or a [template](/guide/templates). With a template, each of its fields is
either:

- a **fixed value** you type now, or
- **from the delivery** (webhook only): pick a field of the delivery and the type its value must
  have: a number, a choice from a list you give (one allowed value per row), a short plain text
  (up to 200 characters), or yes / no.

If a delivery's value doesn't fit its type, that delivery is refused, nothing starts, and the run
history names the field. A field the last delivery had with another type is shown as a problem
before you can go on, for example "pr_number is a number; pull_request.title is text". A field
from your variable library, or a secret, can never come from a delivery.

### 4. Limits

One line shows the limits, for example "one run at a time · at most 48 runs a day · pauses after
3 failures in a row · runs until you turn it off". Open **Change the limits** to set:

- what happens when the previous run is still going (skip it, or run up to N at once);
- the most runs per day;
- how many failures in a row pause it;
- a date it stops on.

### 5. Review & enable

Name it and read the summary. **Save** stores it **off**: nothing runs yet. Then
**Enable…** shows everything it may do on its own, in BattleLab's own words, and you confirm it.
If you later change something that widens what it may do (a new target, bypass on, other events, a
new value from the delivery, a different schedule, a higher limit), saving asks for your consent
again. Until then it keeps the scope you approved.

## Moving between steps

**Next** waits until the step is complete and says what is missing. **Back** keeps your answers. On
a computer you can also jump to any step you have completed from the list on the left. If you leave
with unsaved changes, BattleLab asks first.

## Running and history

The automation's page shows its runs, newest first, including the ones that didn't run and why.
**Run now** runs an enabled schedule or Run-now automation at once. A webhook automation has no Run
now: only a delivery starts it.
