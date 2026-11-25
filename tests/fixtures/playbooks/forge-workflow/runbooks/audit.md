+++
title = "Nightly audit"
trigger = "ritual"
bail = ["a check fails twice with the same cause"]

[caps]
iterations = 3
wall_clock_minutes = 30

[requires]
connections = ["forge"]
+++

1. List the open pull requests on {{repo}}.
2. Report anything older than a week.
