---
name: build-step
description: Build one numbered step of the HCWMA build plan from start to finish. Use when the owner says "build step N", "do the next step", "continue the project" or runs /build-step.
---

# Build one HCWMA step

Step to build: $ARGUMENTS

If no step number was given, open `docs/PROGRESS.md` and take the first unticked step.

## 1. Understand the step

1. Read the step's entry in `docs/BUILD_PLAN.md`: goal, scope and "Done when" test.
2. Read the sections of `docs/MASTER_DOCUMENT.md` that the step points to.
3. Read `docs/DECISIONS.md`. If the step needs a value that is blank, stop and ask the owner.
4. Check that every earlier required step is ticked in `docs/PROGRESS.md`. If not, say which
   one is missing and stop.

## 2. Plan before coding

Tell the owner, in a short list:

- the files you will create or change,
- any new dependency and why,
- how you will prove the "Done when" test.

Wait for the owner's go-ahead if the step downloads large files, installs system software
or touches anything outside the repository.

## 3. Build

- Create a branch `step-<n>-<short-name>`.
- Write the tests for the "Done when" condition first, then the code.
- Stay inside the step's scope. If you notice work that belongs to a later step, note it
  in `docs/PROGRESS.md` under Notes and leave it.
- Follow every architecture rule in `CLAUDE.md`.
- For a new sub-agent use the `add-agent` skill. For a new tool or adapter use `add-tool`.

## 4. Prove it

- Run `pytest -q` and `ruff check .`. Fix failures; never skip or delete a failing test to pass.
- Run the `verify` skill.
- Run the step's "Done when" test for real and show the owner the output. If part of it
  needs the owner (for example speaking into the microphone or clicking an approval
  prompt), give exact instructions and wait for the result.

## 5. Close the step

1. Tick the step in `docs/PROGRESS.md` and add a one-line note (date, what the next step
   should know).
2. Update the Commands section of `CLAUDE.md` if commands changed.
3. Commit with message `step <n>: <what was built>`.
4. Give the owner a summary: what works now, how to try it, what is not done.
5. Stop. Do not begin the next step.

## If something blocks the step

Say exactly what failed and what you tried. Offer the owner options. Do not weaken a
security rule, loosen a permission or fake a test result to get a step finished.
