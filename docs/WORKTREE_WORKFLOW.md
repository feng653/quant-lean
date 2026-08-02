# Task-owned worktree workflow

This repository uses one clean primary worktree for integration and short-lived,
task-owned worktrees for repository changes.

## Invariants

- The primary worktree is the checkout of `master`. It is used only for review,
  integration, and final verification.
- Read-only analysis does not require a task worktree.
- Every writing agent receives its own task worktree. Two agents never write to
  the same worktree.
- A task branch uses `codex/<task>-<agent>-<shortid>`.
- Uncommitted changes are never stashed, reset, cleaned, overwritten, or removed
  by the manager.
- The manager never pushes, force-removes a worktree, or deletes a task branch.
- A worktree may be retired only when it is clean and its patches are present on
  `master`.

## Agent entry flow

1. The lead agent classifies the request as read-only or writing.
2. For a writing request, complete the repository's PRD confirmation step.
3. The lead agent creates the task worktree:

   ```powershell
   .\tools\worktree.ps1 new <task> -Agent <agent>
   ```

4. Give the writer the absolute worktree path, exact branch, base commit, owned
   files, prohibited files, acceptance criteria, and verification commands.
5. Before the first edit, the lead rereads
   [`docs/todo/TODO_INDEX.md`](todo/TODO_INDEX.md) and every referenced TODO from
   disk, then records the task in the owning dated `CODE_TODO_*.md`. On a status
   change, handoff, integration or deployment result, update that same item without
   overwriting a user's manual notes. `docs/EXECUTION_TODO.md` is compatibility-only.
   `ROADMAP.md` changes only when a version goal, order or release fact changes.
6. The writer works only in that worktree, commits its owned changes, and reports
   the commit and verification evidence.
7. The lead agent runs `inspect`, reviews the diff, verifies the result, and then
   runs `integrate`.
8. Retire is a separate, explicit action. It keeps the branch.

## Mandatory completion loop

After every completed item, integration, deployment, resume, or discovered defect:

1. Reread `docs/todo/TODO_INDEX.md` and all referenced TODO files from disk.
2. Preserve user-added items, ordering, notes and acceptance criteria exactly.
3. Select the first executable unfinished item from the earliest code version.
4. Finish code versions through review, integration, deployment and post-deploy
   verification before starting the non-code experiment operations TODO.
5. If an experiment operation exposes a code defect, add a focused patch-version
   code TODO and return to step 1.
6. Stop only when every TODO is complete, or record an external blocker that cannot
   be resolved within the current authorization.

Each version must have one coherent direction. Split diffuse requests into ordered
versions before editing. Apply SemVer based on compatibility: patch for compatible
fixes, minor for compatible capabilities, and major only for intentional breaking
contracts.

Writing agents must refuse to edit when no task worktree was assigned. Read-only
reviewers may inspect any worktree but must not modify it.

## Commands

```powershell
.\tools\worktree.ps1 new <task> -Agent <agent>
.\tools\worktree.ps1 attach <task> -Agent <agent> -WorktreePath <absolute-path>
.\tools\worktree.ps1 status
.\tools\worktree.ps1 status -Json
.\tools\worktree.ps1 inventory -Json
.\tools\worktree.ps1 reconcile
.\tools\worktree.ps1 inspect <task>
.\tools\worktree.ps1 handoff <task> -Agent <agent> -ValidationSummary "tests passed"
.\tools\worktree.ps1 integrate <task>
.\tools\worktree.ps1 retire <task>
.\tools\worktree.ps1 leader -Agent <agent> -Session <session-id>
```

`status`, `inventory`, and `inspect` are read-only. The state directory is under
the Git common directory at `.git/codex-worktrees/`; it is shared by every
worktree and is never committed.

The checked-in configuration starts in `observe` mode so legacy agents can drain
without being seized. In `strict` mode, `new`, `attach`, `integrate`, and `retire`
also require `-LeaderSession <session-id>` matching an unexpired leader lease.
`reconcile` only reports incomplete manager transactions; it never resets,
deletes, or repeats Git operations automatically.

## Existing worktrees

Legacy worktrees begin as unmanaged observations. Their activity owner is
`unknown` until an agent explicitly hands them off or a human assigns ownership.
The first inventory never moves or deletes a worktree or branch.

An existing agent may join management without changing its files or branch:

```powershell
.\tools\worktree.ps1 attach <task> -Agent <agent> -WorktreePath <absolute-path>
```

Classification uses both recorded source-to-target commit mappings and
`git cherry`, because a cherry-picked source commit is not an ancestor of
`master` even though its patch is already integrated.

## One lead across Codex and OpenCode

Both clients use the same state directory. `leader` acquires a renewable lease;
an unexpired lease held by another session is not replaced automatically.
This coordinates future work, but it cannot seize the context or process of an
independently started legacy agent.
