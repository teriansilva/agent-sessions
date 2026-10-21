# Forgejo workflow

Start with an issue, implement on a branch, obtain independent review, merge, deploy and verify
the live change. A changes-requested review returns to implementation for at most **four rounds**;
after that, the intended flow asks the operator. An approval belongs to the exact reviewed head.

## Use this template

Open **Library → Playbooks → Forgejo workflow → Duplicate to local**, then **Edit playbook**.
Choose an available agent and model for the planning and implementation steps. The initial Codex
reference is an example; if unavailable, it remains unresolved until you choose a replacement.
Edit the evidence, dependencies and maximum rework rounds in the step inspector, then save.

**Available now:** browse, duplicate, edit and save the bundle. Saving does not execute its flow.
Automatic step dispatch and review/fix transitions are still being implemented. The checklist
and run order are a reusable specification; they do not authorize background actions.

## Configure the real workflow

Supply your forge address, repository, branch, reviewer account, review trigger, deployment
workflow and live endpoints through the declared variables when binding a project. Examples
are hints, not selected targets. This bundle contains no credentials, credential references,
CI files, agent instruction files or scripts. A private forge may require adding a secret
variable and connection credential reference to your own local bundle before deployment.

Configure the actual reviewer account (for example, Hermes), its permissions and webhook/CI
trigger on the forge separately. Ensure the repository already has the required labels, CI
checks, deployment workflow, secrets and revision endpoint. This template provisions none of
them. Missing prerequisites mean the workflow is not verified.

The existing `forge_review` probe checks authorized reviewers at the current head; it does
**not** select a reviewer by the label or `reviewer_account` variable. The merge step therefore
retains an operator confirmation that the requested reviewer approved this head and all required
checks are present. Keep that check when adapting the template. Approval from another account
does not substitute for the requested reviewer.

Merge and deployment each retain an explicit operator decision. The live revision check uses
the observed **merge SHA**, not the pre-merge branch SHA, so squash merges can be verified.
No missing review, missing revision or health-only result is evidence that the workflow finished.

To extend the process, add a step using an existing actor and evidence probe. An external step
describes work performed by an existing service; its label does not install or invoke that service.
Keep credentials out of briefs, descriptions and README text.
