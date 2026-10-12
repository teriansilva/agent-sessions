# Playbooks

A playbook packages a workflow, its evidence checks and optional project materials. Open
**Library → Playbooks** to browse your installed bundles. Bundled examples are read-only;
**Duplicate to local** creates an editable copy with fresh identifiers and remapped references.

## Start from an example

**Forgejo workflow** describes planning an issue, implementing on a branch, independent review,
merge, deployment and live verification. A changes-requested review returns to implementation
for up to four rounds. Choose your reviewer account, such as an existing Hermes bot, and describe
the CI/webhook trigger you already operate. The template installs neither the reviewer nor its
trigger. A separate operator check before merge confirms that the requested reviewer approved
the exact head and every required check is present.

**Research brief** describes agreeing on a question, drafting with citations and uncertainty,
independent review with up to three revisions, and explicit operator acceptance. It has no
connections or publishing action.

To customize either example:

1. Open its card and choose **Duplicate to local**.
2. Open **Edit playbook**, rename your copy and select a step.
3. Choose an available agent/model or an operator/external actor, then edit its instructions and completion rules.
4. Set dependencies and a bounded rework target. The target must be an earlier dependency, and the
   condition names an evidence check on the reviewing step. The round limit is 1–10.
5. Save. A validation error names the field to correct. A conflicting save preserves your draft
   for comparison or for saving as another new playbook.

The example agents are references. If an agent or model is unavailable on your host, it stays
unresolved until you explicitly select a replacement. Target examples are hints; they do not
silently select a repository or endpoint. Credentials belong in secret bindings, never in a
brief, README or ordinary variable default.

## What runs today

You can browse, duplicate, edit and save these templates. **Saving does not execute the flow.**
Automatic mission step dispatch and review/fix transitions are still being implemented. The
steps describe the intended process; they do not grant permission to merge, deploy, publish or
send messages. Project-file deployment and flow execution are separate operations.

An external actor names work performed by a service you operate; its label does not connect or
invoke that service. The existing review probe checks authorized reviews at the current head,
but does not filter by the requested reviewer username. Keep the Forgejo template's manual
reviewer confirmation and deployment approval when adapting it.

## Create your own

Choose **New playbook** and name it. Workflow properties let you name the workflow and define
its inputs. Add a step, then select its graph node to open **Properties**:

- **Who performs this step** records its agent, external service or operator assignment.
- **Instructions** describes the work. Manual instructions are available; template import is pending.
- **Completion rules** defines the independent evidence required for completion.
- **What happens next** sets prerequisites and bounded review loops. **Starts after**, Move up/down
  and Remove all work without dragging; moving a step changes its display order, not its prerequisites.

Use Tab and Enter/Space to select a graph node. **Back to flowchart** or Escape returns to the
workflow and restores node focus, including on phones. Clicking the background shows workflow
properties. Advanced groups hold technical IDs, outputs and independence constraints.

Saving writes a local bundle; it does not modify projects pinned to an earlier revision or start
a workflow. API profile setup and AI creation are not yet available in this editor.
