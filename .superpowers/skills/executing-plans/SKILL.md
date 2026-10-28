---
name: executing-plans
description: >
  Execute multi-step implementation plans (superpowers plan docs, numbered
  Task N specs) with discipline: read the whole plan, follow TDD
  red-green-refactor, verify each step, and report progress honestly.
  Use whenever a plan document or numbered task list must be carried out.
license: MIT
---

# Executing Plans

Plans exist because the work is bigger than one thought. Execute them the way
a senior engineer executes a spec: read first, change second, verify always.

## Before you start
- Read the ENTIRE plan doc before touching code. Find the current task (e.g.
  "Task 11") and its exact spec: files, interfaces, tests, verification
  commands, and commit requirements.
- Check the workspace state: does the code match "tasks N-1 complete"? If a
  prerequisite is missing, build it first or say so — never pretend the
  ground exists.
- Read the actual functions you must change. The plan's interfaces are
  truth; your memory of the code is not.

## During execution
- TDD discipline: write or update the failing test FIRST (red), run it to
  confirm it fails, then implement (green), then refactor if needed.
- One task = one focused change set. Do not scope-creep adjacent files.
- Preserve existing conventions (import style, marker protocols, error
  handling) — a plan change that fights the codebase is a second bug.
- Verify with the commands the plan specifies (e.g. the isolated test
  container), not just local happy paths.

## After each task
- Run the task's tests, then a regression sweep of adjacent tests.
- Commit atomically with the repo's required trailers (check recent commits
  for the convention, e.g. `Co-Authored-By`, `Claude-Session`).
- Report: what changed (exact lines/files), what was verified, and what is
  blocked or deviates from the plan and why.

## Anti-patterns
- Skipping the red step ("it is a small change, tests are overkill").
- Patching only the path the ticket names while sibling callers stay broken.
- Shipping a change that cannot be verified in the specified environment.
