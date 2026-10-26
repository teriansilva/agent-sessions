# Specialists

A **specialist** is a job you can hand work to: a title (*Intern*, *Implementer*, *Reviewer*,
*Bookkeeper*), what it does, the skills it brings, the agent and model it runs on, and how many can
work at once. Specialists live in **Library → Specialists**, the pool your missions will be staffed
from. A specialist runs *on* one of your agents; it doesn't replace it.

The pool starts empty. BattleLab creates nothing for you.

## Create one

1. Open **Library → Specialists** and choose **New specialist**.
2. Start from a template (Intern, Implementer, Reviewer or Bookkeeper) or from scratch. A template
   fills in the job: the title, a summary and suggested skills. The agent and the model are always
   yours to pick.
3. Choose the **agent** and **model**. For an API client you can also pick an effort level.
4. Add **skills** from your skills library, set the **capacity** (1 to 8 at once), and save.

Only agents that can load BattleLab's tools can carry skills. The agent list marks the others with
"cannot load skills", and the editor won't save a specialist with skills on one of them.

## Versions

Saving an existing specialist makes a new version. Older versions never change, so anything that
was approved with version 3 keeps version 3. Archiving a specialist hides it from new use; its
versions stay readable.

A skill is saved at the version your library has right now. If you edit that skill later, the card
says **changed in your library**. Open the specialist and save it to pick up the new version.

## Grades

Every specialist shows a grade: **Intern** (fast and cheap), **Specialist** (strong) or **Senior**
(frontier). You never type it: it comes from the specialist's **model**, through your own map in
**Settings → Agents → *the agent* → Model grades**.

That map starts empty, because BattleLab can't know how you rate a model. Until you grade a model,
specialists on it show **Ungraded**. A procedure step that asks for a minimum grade can't use an
ungraded specialist; a step that asks for any grade can. "Agent default" is never graded, because
it isn't one model.

Changing a grade re-grades every specialist on that model at once.

## Specialist notes

A specialist keeps its own notes **for each project**: what it learned working there, separate from
the project's shared notes. They live in the project's memory, follow the project's memory switch,
and never travel to another project. Open **Library → Memory**, pick the project, and its
specialist notebooks are listed beside its own notes.

## Coming next

Capacity limits and starting a session from a specialist (which brings its skills and its notes
into that session) are on their way.
