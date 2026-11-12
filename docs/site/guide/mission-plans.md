# Mission plans

A **mission plan** is what a mission achieves and in what order, kept for reuse: its objectives,
the order between them, the procedure each objective follows, and the **project slots** those
objectives work in. Every step says which skills it asks for and the minimum grade it needs.

A mission plan never names one of your real projects, and it runs nothing by itself. Mission plans
live in **Library → Mission plans**.

## Save a mission's plan

1. Open a mission that has a plan. A plan comes from applying procedures to the mission.
2. Open its **⋯** menu and choose **Save as mission plan**.
3. Give the plan a title and, if you want, a summary.
4. Name each **project slot**. Every project the mission used becomes one slot, numbered in the order
   its objectives first appear. Name the slot by its role, such as *Mail*, *Files* or *Finance*. The
   dialog shows which of your projects each slot stands for right now, but that name is not saved.
5. Choose **Save to Library**.

**Never saved in a plan:**
- which projects, folders or repositories the mission used;
- input values: branches, pull requests, numbers, and playbook defaults;
- the mission's instruction and brief;
- evidence, results and sessions.

The procedures are saved as written, the same way a playbook export carries them.

## In the Library

Each plan shows how many objectives, slots and steps it has, and whether it was **saved from a
mission** or **imported from a file**. Open a plan to see:
- its slots;
- its objectives in order, each with its procedure and the objectives it comes after;
- every step, with its actor and its role needs.

A saved plan never changes. **Archive** hides a plan; it is never deleted. **Show archived** adds the
archived plans back to the list, and **Unarchive** brings one back.

## Share one

Mission plans travel in the same `.battlelab` files as specialists and skills (see
[Sharing](/guide/sharing)):
- **Export:** choose **Export** on a plan, or **Export…** on the page. A plan whose title, summary or
  slot labels hold one of your stored secrets can't be exported.
- **Import:** choose **Import…** on the page. The preview checks every procedure in the file. A plan
  you import becomes a **new copy** in your Library, and nothing else happens.

## What a plan doesn't do (yet)

Nothing plans, starts or approves a mission from a mission plan yet. Using a plan to plan a mission
with HQ, where each slot is filled with one of your projects and you approve the mission before
anything starts, comes in a later release. Publishing plans to the public gallery comes later too.
