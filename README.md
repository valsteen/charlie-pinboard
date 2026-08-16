# Codex Repository Work

[![CI](https://github.com/valsteen/codex-repo-work/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/codex-repo-work/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)

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

## Runtime and development

The package targets Python 3.14 only and has no runtime dependencies outside the standard library. `.python-version` pins the current stable 3.14 patch release. uv owns Python acquisition, the project environment, dependency resolution, the checked-in lockfile, and installed-package execution. The command and launcher support macOS and Linux.

```sh
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked pyrefly coverage check src --strict --fail-under 100
uv run --locked coverage run -m unittest discover -v
uv run --locked coverage report
uv build --no-sources
scripts/repo-work --help
```

Local checks, CI, and the plugin launcher all resolve the uv-installed package directly. The checked-in uv lockfile is the single development dependency record.

Repository-owned metadata checks and the current Codex plugin and skill validators provide release evidence for the plugin bundle.
