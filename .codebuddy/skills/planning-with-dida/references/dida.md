# Dida365 mapping

> 2026-11-12 · planning-with-dida 3.28.2-dida.1

## Why Dida is the source of truth

The upstream skill kept every task in `task_plan.md`. That survives context loss,
but the task list is invisible outside the terminal and cannot be checked off
from a phone. This adaptation splits the responsibilities:

| Layer | Owns | Why |
|---|---|---|
| Dida | Phase list, phase status, parent/child structure | Visible on phone, shared across sessions, native completion |
| `task_plan.md` | A projection of Dida + goal/next-step/decisions | Hooks can inject it without a network call |
| `findings.md`, `progress.md` | Research and session log | Too large/noisy for Dida; never uploaded |

The hook pulls from Dida before each user turn. The file is a cache; editing
`**Status:**` by hand is overwritten on the next pull.

## Prerequisites

```bash
dida auth status          # must be logged in
dida project list --json  # must contain a list named Project
```

If the token is missing, follow the `滴答清单` skill: create an API token at
Dida web → avatar → settings → account & security → API token, then
`dida auth token <token>`. Never put the token in a plan, commit, or reply.

Set `PWF_DIDA=0` to disable the integration for one session. The file-based
planning loop still works; statuses simply stay local.

## Task shape

Parent task:

- List: `Project` (or `DIDA_PROJECT_ID` / `--project`).
- Title: `计划：<plan name>` (`MAX_TITLE` = 40 characters).
- Tags: none. Parent status is the roll-up; do not tag it.
- Body:

```text
计划：<relative path to task_plan.md>
目标：<goal>
下一步：<next action>
阶段：<completed>/<total>
```

Phase subtask:

- Title: `P<n> <phase name>` (40 characters max).
- Parent: the plan parent task.
- Tags: `进行中` when the phase is in progress; no tag when pending.
- Status: Dida status 2 when complete.
- Body:

```text
计划：<relative path>
阶段：<n>/<total>
```

`#` followed by a digit is escaped to `＃` because Dida parses `#12` as a tag.

## Commands

All commands accept `--plan <path>`; without it the active plan is resolved the
same way as the hooks (`PLAN_ID` → `.planning/.active_plan` → newest plan →
`./task_plan.md`).

```bash
# After filling Goal and Next Step in a new plan
python3 scripts/dida-sync.py init --name "Backend Refactor"

# Re-push titles/statuses after editing the plan locally
python3 scripts/dida-sync.py sync

# Pull phone-side check-offs into the local cache
python3 scripts/dida-sync.py pull

# Move one phase
python3 scripts/dida-sync.py phase 2 in_progress
python3 scripts/dida-sync.py phase 2 complete

# Update the next action in the plan and the parent task
python3 scripts/dida-sync.py next "Review PR #278, then merge"

# Complete every phase and the parent
python3 scripts/dida-sync.py complete

# Inspect the mapping
python3 scripts/dida-sync.py status --json

# Rebuild task_plan.md after losing the local file
python3 scripts/dida-sync.py restore --parent <taskId>
```

`init` and `sync` are idempotent: they look up the parent by `.dida.json`,
then by exact title + plan path, then by child `P<n>` titles. A second run
does not create duplicates.

## Lifecycle

### New plan

1. `init-session.sh "<name>"` creates `task_plan.md`, `findings.md`, `progress.md`.
2. Fill `## Goal`, `## Next Step`, and the phase titles.
3. Run `dida-sync.py init --name "<name>"`.
4. Work. Use `phase` when a phase starts or finishes.

### Resume

1. The hook runs `dida-sync.py pull --quiet --max-age 15` before injection.
2. Read `task_plan.md`, `findings.md`, and `progress.md`.
3. If the hook is unavailable, run `pull` manually first.

### Phone check-off

Check the `P<n>` subtask in Dida. The next user turn pulls status 2 into the
local plan; `## Current Phase` advances to the next incomplete phase. Checking
the parent completes every phase (parent status 2 is treated as all complete).

### Finish

`dida-sync.py complete` completes remaining children, the parent, and writes
`All phases complete` locally. Do not complete the parent first if you still
want to record per-phase completion times.

### Recovery

- `task_plan.md` lost, `.dida.json` present: `restore` rebuilds the projection.
- Both lost, Dida parent known: `restore --parent <taskId> --force`.
- `.dida.json` lost, plan present: `init` searches Dida by title + plan path;
  check `status` afterwards for duplicates.

## Failure modes

| Symptom | Behavior | Fix |
|---|---|---|
| `dida` not on PATH | `init`/commands exit 1; hook pull is a no-op | Install `@suibiji/dida-cli`, then `dida auth login` |
| Token expired | Commands fail with the CLI error; hook stays quiet | `dida auth token <token>` |
| Dida unreachable | Hook pull is a no-op; cached plan still injects | Keep working locally; pull again when back |
| `Project` list missing | `init` exits 1 with a clear message | Create/rename the list, or pass `--project <id>` |
| Parent completed early | `init` refuses unless `--force` | Reopen the parent in Dida, or `--force` to create a new one |
| Extra phone tasks | Pull ignores them; no phase is added | Plan structure is local; add phases to `task_plan.md` and `sync` |
| Phase title edited on phone | Pull updates the local `### Phase N:` title | Expected: Dida owns titles |
| Status edited locally | Next pull overwrites it | Use `phase`, not a text edit |

## Boundaries

- Only the plan title, goal, next-step, phase names, and statuses are sent to
  Dida. `findings.md`, `progress.md`, code, and command output never leave the
  machine through this skill.
- Phase count and phase order are owned by `task_plan.md`. Phone edits can
  change status and titles, but an extra Dida subtask is not adopted as a phase.
- The 15-second pull cache is per plan (`lastPull` in `.dida.json`). Use
  `--force` for an immediate refresh.
- Attestation: when the plan has `.attestation` or `.plan-attestation`,
  `pull`/`phase`/`next`/`complete` re-run `attest-plan.sh` after writing so v3
  gated mode does not block on a stale hash.
