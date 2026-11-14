---
description: Review the current changes for bugs
argument-hint: "[what to focus on]"
---
Review the uncommitted changes in this repository (run `git status` and `git diff`, plus `git diff --staged`).
Look for real defects: wrong logic, unhandled cases, broken callers, security problems, missing tests for new behaviour.
Ignore style unless it hides a bug. Focus: $ARGUMENTS

Report findings most severe first, each with file:line, what goes wrong, and a concrete fix. If you find nothing, say so plainly. Do not change any files.
