# Pinboard

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<img align="right" width="430" src="assets/pinboard-investigation-board.png" alt="Pixel-art bard explaining a fantasy investigation board covered with maps, clues, and red thread">

Long-running coding-agent work rarely stays in one conversation. A feature uncovers a prerequisite, a review sends implementation back for correction, an interruption leaves work unfinished, and the next task has to recover what the project actually decided.

Pinboard gives one repository a durable work ledger. Proposed work, priorities, dependencies, accepted briefs, current ownership, pauses, and review outcomes stay consistent across Codex tasks. A concern can survive the conversation that uncovered it, and an implementation can reach another task without its brief being retold.

Pinboard does not decide the product, choose priorities, or create Codex tasks. It records project decisions, rejects changes that no longer fit the current state, and keeps current ownership explicit. A short isolated change probably does not need it. Work that spans tasks, interruptions, dependencies, or independent review often does.

## A campaign that survives the conversation

Imagine you are building *Ashfall Keep*, a small action RPG. One Codex task is working on the dragon boss's second phase. Two scouts return with useful but distracting discoveries: save games capture temporary animation state, and controller mappings identify abilities by inventory position.

Without shared project state, the original conversation becomes an accidental backlog. Useful conclusions disappear into old tasks, and a focused feature quietly turns into several unrelated changes. Pinboard gives each concern an explicit place while keeping the current work honest.

![Pixel-art quest scroll](assets/quest-scroll.png) **Preserve proposed work without changing the plan.** The scout invokes `$pinboard-intake` to record the save-game problem with its trigger, evidence, and likely consequence. It enters intake, the state for unstarted work awaiting a decision. It does not become ready, replace the current focus, or interrupt the dragon attempt. A coordinating task can later decide whether it belongs in the plan.

The scout gets a compact receipt:

> **Saved for later — the animation-state concern is now in `save-game-animation-state` (`intake`); dragon work continues.**

<img align="right" width="390" src="assets/party-crossroads.png" alt="Pixel-art bard and adventurers considering routes toward a river village, a dragon keep, and a crystal cave">

![Pixel-art open quest ledger](assets/quick-quest-log.png) **See the current work before deciding what moves.** Ask `$pinboard` where things stand and it reads one revision-stamped live overview: the dragon phase is active, controller mapping is ready, and the save-game work remains in intake. The coordinating task can mark work ready, defer it, close it, connect a dependency, or preview which independent items could move together. Reading the map does not change it.

![Pixel-art campfire checkpoint](assets/safe-camp.png) **Pause and resume without reconstructing the journey.** If stable ability IDs become a real prerequisite, the dragon attempt records where it stopped, why it cannot continue, and what would make it resumable. After the prerequisite is completed and accepted, the same attempt resumes from preserved evidence instead of asking a new conversation to infer the missing history.

Resume restores paused or blocked work, returning a retained attempt to active or unstarted work to ready. Reopen is different: it returns deferred work to intake for reconsideration. Continue only confirms that an already-active attempt proceeds; it does not change lifecycle state.

![Pixel-art crossed sword and hammer](assets/ready-to-build.png) **Deliver the work that was actually accepted.** Before reading implementation sources, coordination gives one task a renewable preparation claim pinned to the ready item's exact definition. The item stays ready while that task publishes and reviews the canonical brief. Activation consumes the claim and creates the attempt atomically; `$pinboard-deliver` then claims that active attempt, follows the exact definition and checks, and records the result for review by a separate Codex reviewer. If the definition changes after preparation stops, coordination transfers a fresh claim and republishes the matching brief before activation.

The code, branch, and conversation remain ordinary repository work. Pinboard keeps the decisions and execution around them durable. [How Pinboard works](HOW_IT_WORKS.md) follows these ideas through the product, package layers, and relational ledger.

## What it covers

- **Intake:** preserve a discovery without silently changing priority or starting work.
- **Planning:** make readiness, deferral, closure, dependencies, and current focus explicit.
- **Revisioned definitions:** replace a complete accepted definition with compare-and-swap safety, retain every prior revision, and inspect current or paginated history as typed JSON.
- **Execution:** give each accepted attempt an exact brief and independent renewable ownership.
- **Interruption and recovery:** block, pause, resume, or recover work without rebuilding its context from chat history.
- **Parallel work:** preview independent items and recheck the group as each attempt starts, without creating tasks on the user's behalf.
- **Review:** keep the submitted candidate and its evidence exact, then require a separate task to accept it or return it for correction.
- **Handover:** export one complete revision-stamped JSON package containing admitted work, pending proposals, relationships, decisions, and verified review evidence without choosing a team-tool vendor.
- **Recursive cleanup:** use `$slop-cleanup` to trace residue from revised or abandoned features, remove an approved family, and repeat until a fresh pass finds nothing new.

The `$pinboard`, `$pinboard-intake`, and `$pinboard-deliver` skills provide the conversational workflows. The `pinboard` command validates and updates the repository-local ledger, rejects stale actions, and keeps unrelated attempts from invalidating one another.

Private working state stays in ignored local files:

```text
.codex/pinboard/
  state.sqlite3               # authoritative lifecycle, dependencies, leases, and history
  artifacts/                  # immutable briefs, evidence, proposals, and reviews
  views/                      # generated human-readable projections
```

SQLite is the current ledger authority. Commands read its complete typed state, then commit only the relations named by one accepted mutation; stale or failed changes leave the prior ledger intact. Immutable artifacts retain long-form contracts and review evidence; generated views are convenient projections, not fallback state. The [architecture map](ARCHITECTURE.md) explains package ownership, persistence boundaries, and failure semantics for contributors and agents.

The installed definition commands are:

```sh
pinboard item definition --item-id <item> --json
pinboard item definition-history --item-id <item> --limit 20 --json
pinboard item revise --file <pinboard-item-revision-v1.json> --task-id <task> --host-id <host> --json
```

Revision files replace the whole `pinboard-work-item-definition/v1`; partial patches are rejected. Blocking can only name dependencies already present in that definition and never changes accepted dependencies itself.

Run `pinboard handover --json` to materialize the strict `pinboard-project-handover/v1` document. The command reads one validated SQLite snapshot, verifies every accepted immutable artifact, embeds its exact bytes as UTF-8 text or base64, and writes nothing unless the complete package is ready.

## Install from GitHub

The plugin currently supports macOS and Linux. It uses [uv](https://docs.astral.sh/uv/) to provide its Python 3.14 runtime and installed command.

Add this repository as a Codex marketplace, then install the plugin:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository and ask:

> Set up the pinboard here and explain how I can use it from one chat or several chats.

When another task uncovers something worth keeping, ask it:

> Add this to the repository work queue as intake: saving a boss fight currently captures temporary animation state. Include what you found and why it could block phase-two save support.

For a quick current picture, ask:

> Give me the quick live-work overview. Then offer the deeper views I can ask for.

## Runtime and development

The package targets Python 3.14 only. msgspec provides immutable records and strict JSON decoding at repository boundaries. uv manages Python installation, the project environment, dependencies, the checked-in lockfile, and command execution.

```sh
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked pyrefly coverage check src --strict --fail-under 100
uv run --locked coverage run -m unittest discover -v
uv run --locked coverage report
uv run --locked python scripts/validate-metadata.py
uv run --locked python -m docs.how_it_works.render --check
uv build --no-sources
scripts/pinboard --help
```

Local checks, CI, and the plugin launcher all use the package installed by uv. The checked-in uv lockfile is the single development dependency record. CI validates the plugin and its skills before the repository is published.
