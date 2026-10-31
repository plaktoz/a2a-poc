# Plan: work on a real repo (P10)

Date: 2026-10-31

Status: open questions answered by the owner (2026-10-31). Slice 1 starts after P19. No code is written.

## 1. Goal

A person points the factory at a git folder and gives a requirement. The factory changes the code on its own branch. It commits the change.
It tells the person to review the commit.

This plan covers PRD FR-9 and FR-10. It also covers the PRD line "one branch or worktree per task".
The decisions come from section 5 of `docs/handoff.md`.

## 2. What exists today

| Part | Today | Effect on P10 |
|---|---|---|
| `open_workspace` (`engine.py:57`) | Creates `apps/<name>` and locks `.factory/lock` with `flock`. | The lock is already per folder. One folder per repo and feature gives a lock per repo and feature. |
| `Engine.ws` | One folder holds the run state (`.factory/`) and the built code. | Run state must stay outside the worktree. See section 3.1. |
| `cwd: target` (`engine.py:519`) | A step can run in an existing path. Only `code-audit` uses it. | The new runbook reuses it. |
| `_check_files` (`engine.py:413`) | Looks up `plan.templates[item.surface]` to decide which files an agent can change. | A real repo has no template. A new write mode is needed. |
| `snapshot` (`engine.py:84`) | Reads every file under the folder into memory. | Too slow on a real repo. Git detects the changes instead. |
| Gates (`gates.py`) | The test gates run the commands of a scaffold template. | A real repo has its own test command. See open question 1. |
| Workers (`invoke.py:109`) | `codex` runs in an OS sandbox. `claude` has no OS sandbox. Its limits are `cwd`, the tool allowlist and `--permission-mode acceptEdits`. | Only the `codex` worker is sandboxed. Section 6 covers the `claude` worker. |

## 3. Design

### 3.1 Folder layout

```
~/factory-scratch/<repo>/<feature>/
    .factory/        run.json, artifacts.json, lock, agent logs
    tree/            git worktree of the repo, on branch factory/<feature>
```

- `<repo>` is the folder name of the git repo.
- `<feature>` comes from `name_from(brief)` or from `--name`.
- The `Engine` workspace is `<feature>/`. The target is `<feature>/tree/`.
- The scratch root comes from the environment variable `FACTORY_SCRATCH`. The default is `~/factory-scratch`.
- The lock is `<feature>/.factory/lock`. Two runs of one feature collide.
- Two features of one repo run together. Each has its own lock.

### 3.2 Worktree

1. Check that the path is a git repo: `git -C <repo> rev-parse --show-toplevel`.
2. Pick the base. The default is `HEAD` of the repo. `--base <ref>` overrides it.
3. Create the worktree: `git -C <repo> worktree add -b factory/<feature> <tree> <base>`.
4. Stop with a `ConfigError` if the branch exists. Tell the person to choose another `--name`.

Uncommitted changes in the repo are not in the worktree. The factory warns when the repo has them.

### 3.3 Runbook

A new runbook `change-request` has `needs_target: true`. It has these steps:

| Step | Role | What it does |
|---|---|---|
| `spec` | analyst | Turns the brief into acceptance criteria. Has a checkpoint. |
| `build` (loop) | developer | Changes the code in the worktree. |
| `verify` (in the loop) | reviewer | Reads the diff and the test result. Approves or sends feedback. |

The runbook reuses the gate `review_approves`. It adds one gate, `repo_tests_pass`.

### 3.4 Write guard

- Add the write mode `writes: target` to `StepDef`. It allows any path in the worktree except `.git`.
- `_check_files` skips the template lookup for this mode.
- `Engine` lists the changed files with `git status --porcelain` in the worktree. It does not use `snapshot`.
- The guard that reverts test edits by a source step does not apply.
  The repo has no known test-file pattern.

### 3.5 Tests gate

`repo_tests_pass` runs one shell command in the worktree. The orchestrator runs the command itself.
The gate never trusts a count that an agent reports.
The command comes from `--test-command`, or from detection (open question 1).

### 3.6 Commit

After the loop passes, the factory runs these commands in the worktree:

1. `git add -A`
2. `git commit -m "<title from the brief>"`. The body lists the acceptance criteria and the run ID.

The factory prints the branch name, the worktree path and `git diff <base>...factory/<feature>`.
It does not push or merge.

## 4. Slices

### Slice 1: a worktree and a commit

| Item | Detail |
|---|---|
| Code | New `repo.py` for the worktree, the base and the commit. `writes: target`. Gate `repo_tests_pass`. Runbook `change-request`. CLI flags `--repo`, `--base`, `--test-command`. |
| Record in `run.json` | `repo`, `base`, `branch`, `tree`, `commit`. |
| Done when | A run on a test repo ends with one commit on `factory/<feature>`. The original checkout is unchanged. Two features of one repo run at the same time. |

### Slice 2: pull request

| Item | Detail |
|---|---|
| Code | `--pr` flag. Runs `git push -u origin factory/<feature>`, then `gh pr create`. |
| Why a flag | A push leaves the machine. It needs an explicit request. |
| Approval | Without `--yes`, the run waits at a checkpoint with `awaiting: approval`. This is decision 8 of the PRD. The run records who approved. |
| Done when | The PR body holds the acceptance criteria, the gate results and the usage totals. |

## 5. Tests

Tests use a real git repo in `tmp_path`. They use the fake agents of the existing tests.

| Test | Checks |
|---|---|
| Worktree on the new branch | The branch and the path exist. |
| Branch name collision | `ConfigError`. No worktree is made. |
| Original checkout untouched | `git status` of the repo is the same before and after. |
| Two features in parallel | Two locks. Two branches. No error. |
| Same feature twice | The second run fails on the lock. |
| Write guard | A change to `.git` is reverted. A change to a source file is kept. |
| Tests gate | A failing command fails the gate and shows its output. |
| Commit | One commit. The body holds the run ID. |
| Non-git folder | `ConfigError`. |
| Dirty repo | A warning is printed. |

## 6. Risks

| Risk | Mitigation |
|---|---|
| The `claude` worker writes outside the worktree. The allowlist names tools, not paths. I have not checked what the `claude` CLI allows outside `cwd`. | Set `cwd` to the worktree. Add a check after each step: `git status` must show changes only inside the worktree. Revert anything else. Test this with a fake agent. Check the CLI behaviour before slice 1 is accepted. |
| The `claude` worker runs a `Bash(...)` command outside the worktree. The allowlist matches a prefix only. | Same check as above. This does not stop a command that reads files. Say so in the docs. |
| An agent commits a secret. | Add the secret scan (P6) before slice 2. See open question 3. |
| Large repos make `snapshot` slow. | Slice 1 uses git for change detection. |
| Worktrees pile up in the scratch folder. | Slice 1 prints the cleanup command, `git worktree remove <tree>`. A `factory clean` command comes later. |
| Agents use the network to install packages. | The existing `tools.network` rule applies. No change. |

## 7. Docs to update when the code lands

Update `README.md`.
Cover the new flags, the runbook, the folder layout and the test count.
Update the P10 row in `docs/code-review.md` and the status in `docs/software-factory-prd.md`.

## 8. Open questions

Decided by the owner on 2026-10-31.

| # | Question | Decision |
|---|---|---|
| 1 | How does the factory find the test command of the repo? | Slice 1 requires `--test-command` and stops with a clear error when it is missing. Detection from `package.json` or `pyproject.toml` comes later. A wrong guess gives a false green. |
| 2 | Which base does the branch start from? | `HEAD` of the repo. `--base` overrides it. |
| 3 | Is the secret scan (P6) a precondition? | Yes. Build it first. It is a regex pass over the changed files. It is a precondition of slice 2. |
| 4 | Where does the scratch root live? | `~/factory-scratch`. `FACTORY_SCRATCH` overrides it. |
| 5 | Does the commit need a signature? | No. The factory makes a plain commit with the repo's git identity. It does not sign the commit. The commit body names the factory run. |
| 6 | Is a `claude`-worker step allowed to write outside the worktree? | No. A step must not write outside the worktree. If it does, the factory reverts the change and fails the step. See section 6. The `claude` worker has no OS sandbox, so the check after each step is the only control. |

## 9. Task checklist

Tick each item when its code and tests pass. An agent that restarts the work continues from the first open item.
The status of the whole slice is in `docs/tasks.md`.

### Slice 1

- [ ] `repo.py`: check the git repo, pick the base, warn on a dirty repo.
- [ ] `repo.py`: create the worktree on `factory/<feature>`. Fail on a branch that exists.
- [ ] Scratch layout `~/factory-scratch/<repo>/<feature>/{.factory,tree}`. `FACTORY_SCRATCH` override.
- [ ] `writes: target` in `StepDef`. `_check_files` skips the template lookup.
- [ ] Change detection with `git status --porcelain`.
- [ ] Revert a write outside the worktree, or in `.git`, and fail the step.
- [ ] Gate `repo_tests_pass`. `--test-command` is required.
- [ ] Runbook `change-request`: `spec`, then the `build` loop with `verify`.
- [ ] Commit: `git add -A`, a plain commit, the body names the run ID.
- [ ] `run.json` records `repo`, `base`, `branch`, `tree`, `commit`.
- [ ] CLI flags `--repo`, `--base`, `--test-command`. Print the branch, the tree, the diff command and the cleanup command.
- [ ] Tests in section 5, with a real git repo in `tmp_path`.
- [ ] Verify with fake agents: one commit on the branch, the original checkout unchanged, two features at once.
- [ ] README, `docs/code-review.md` P10 row, PRD status, test count.
- [ ] Commit.

### Slice 2

- [ ] `--pr`: `git push -u origin factory/<feature>`, then `gh pr create`.
- [ ] Without `--yes`, wait at a checkpoint with `awaiting: approval`. Record who approved.
- [ ] The PR body holds the acceptance criteria, the gate results and the usage totals.
- [ ] Tests with fakes only. No real push.
- [ ] README and test count. Commit.
