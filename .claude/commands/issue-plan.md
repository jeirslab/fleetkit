---
description: Plan one issue of this repo and post the plan on it. Changes no code.
argument-hint: <issue number>
---

Plan issue #$ARGUMENTS of this repository. You change no code and start no workflow in this step.

## Read

- `gh issue view $ARGUMENTS --comments`, and every issue it links to.
- `README.md`, and the files the issue is about.
- The execution templates in `.claude/workflows/` (each file's `meta.description` and `meta.whenToUse`).

## Decide

1. **Is it one task?** If the issue is really several, say so and propose the split (one new issue per task) instead of a plan.
2. **What kind of task is it?** small change, refactor, new feature, or breaking change (something a consumer has to adapt to: the tenant repo, locked flake inputs, option names other files use).
3. **Which template runs it, and how many agents is that?**
   - `issue-small`: one part, nothing breaks. 2 agents.
   - `issue-larger`: two or three parts whose files do not overlap. 4 to 5 agents; `breaking: true` adds one reviewer.
   - None fits? Do not stretch one. Write a new template in `.claude/workflows/<name>.js`, scoped to this kind of task, so the next issue of the same kind can reuse it. Give it a `meta.whenToUse` that says when to pick it. Prefer adjusting an existing template over adding a near-duplicate, and say in the plan why none fit. Every template keeps the hard rules of the existing ones (outside worktree, evaluation only, no deploy, no merge, no production credentials, `issue-review` before converge) and returns `{ issue, branch, worktree, approved }` so `issue-converge` can take it.
4. **Anything you cannot decide from the issue and the code** is a question for the owner, not a guess.

## Post

One comment on the issue, starting with the line `## Plan`:

- **Kind** and **template** (with the agent count and models; if the template is new, its file name and why it was needed).
- **Parts**: for each, a title, the exact files it owns, what to do, and a command that checks it. For `issue-larger` these become its `parts` argument, so files must not overlap between parts.
- **Acceptance checks**: what must be true for the issue to be handled. The reviewer checks each one.
- **Out of scope**: what this issue deliberately leaves alone.
- **Questions**, if any.

Then stop. Execution starts when the owner approves the plan (the `agent-ready` label on the issue, or by saying so).

## Running an approved plan

Every template takes `{ repo, checkout, gates, issue }`; pass the script by path:

- `repo`: `jeirslab/fleetkit`, `checkout`: the main checkout of this repo (`~/fleetkit`), `gates`: `bash tools/gates.sh`.
- Several issues can run at once; each works in `~/worktrees/fleetkit/issue-<n>` on branch `issue/<n>`.
- When they have returned, pass the approved ones to `issue-converge` as `issues: [{ issue, branch, worktree }]`. It lands them on `unstable`, closes the issues, and judges whether to promote `unstable` to `stable`.
