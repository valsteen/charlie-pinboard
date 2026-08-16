# Codex Repository Work

Codex Repository Work coordinates one project-owned local work ledger without turning topic folders, branches, or chat transcripts into competing backlogs.

The repository contains:

- `repo-work`, a standard-library Python command that validates work state, lists legal contextual actions, and applies narrow stale-safe transitions;
- `$repo-work`, the coordinator and orientation skill;
- `$repo-work-intake`, an intake skill that persists proposals separately from admission;
- `$bounded-implementer`, an execution skill for one accepted attempt;
- a Codex plugin manifest that distributes the command and skills as one unit.

## Core model

Work is globally queued. Knowledge is thematically organized. Branches and worktrees belong to execution attempts.

The initial schema uses ignored project-local state:

```text
.codex/work/
  current.md
  queue.md
  coordinator.json
  inbox/
  items/
  attempts/
  history/
.codex/topics/
```

Markdown remains readable and reviewable. The command enforces structural validity, coordinator generations, stale revisions, and legal transitions. Human and agent judgment still owns product meaning, evidence, priority, and scope.

## Development

The package supports Python 3.11 and newer and has no runtime dependencies outside the standard library.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
MYPYPATH=src mypy --strict src/repo_work
python3 scripts/repo-work --help
```

Plugin and skill metadata must also pass the current Codex plugin and skill validators before release.
