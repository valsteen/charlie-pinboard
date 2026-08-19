# Codex Repository Work

[![CI](https://github.com/valsteen/codex-repo-work/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/codex-repo-work/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Keep one trustworthy quest log—and one trustworthy execution brief—while several Codex tasks explore and build in the same repository.

Codex Repository Work helps when a feature stops being a straight line. Side tasks can bring back useful discoveries without turning every chat transcript, topic folder, or feature branch into another backlog. When a piece of work is ready, the same workflow keeps its scope and checks intact from planning through implementation and review.

## An indie RPG feature becomes a campaign

Imagine you are building *Ashfall Keep*, a small action RPG. One Codex task is working on the dragon boss's second phase. While tracing the combat code, you open two other tasks: one finds that save games capture temporary animation state, while the other discovers that controller mappings identify abilities by their inventory position.

All of that is worth keeping, but not all of it should derail the boss feature. Without one shared place for the work, the original task becomes an accidental backlog, useful conclusions disappear into old conversations, and a feature branch slowly turns into a migration branch.

![Pixel-art quest scroll](assets/quest-scroll.png) **A quest is discovered.** A side task comes back with a warning: “Before we can save the second phase safely, combat abilities need IDs that won’t change underneath us.” The proposal stays in the shared inbox until any chat briefly borrows coordination authority and reviews it; the work list does not change behind your back.

**The map fills in as the party travels.** Deep work rarely reveals the whole campaign upfront. While the dragon fight remains the main quest, an experiment may reveal that save games are capturing temporary animation state, a flaw worth returning to later. Repository Work lets the plan grow from that evidence without turning every discovery into an interruption: preserve the exact finding, keep the current objective moving when it is not blocked, and leave one compact receipt.

> **Durable finding — recorded now in `save-game-animation-state` (`intake`); current work not blocked.**

That line is small on purpose. It removes the anxiety that a valuable observation vanished into conversation while keeping the discussion centered on the work in front of you. Over a long investigation, those receipts let the project adapt its plan as reality becomes clearer without reconstructing the journey from old chats.

**The party can split up without walking into the same trap.** Before launching anything, you ask which quests can move together. The preview puts save-game animation research and controller-remapping research in the safe group, explains that a dragon-arena integration still depends on the boss work, and keeps two experiments that need the same capture rig in a “choose one” group. Merely looking at that map creates no new tasks.

You can select a few quests or say, “Launch all safe work.” The animation question still needs design choices, so it opens as a visible task where you can inspect the work and answer questions. The controller investigation already has a complete autonomous brief, so a subagent can scout it quietly and return evidence. Before each launch, the remaining group is checked again; if another task claims the capture rig or changes a dependency, the rest stop with a precise partial result instead of pretending the whole party departed.

![Pixel-art campfire checkpoint](assets/safe-camp.png) **The party makes camp.** The chat making the scheduling change briefly acquires coordination, records that stable ability IDs come first, then releases it. The dragon task notes exactly where it stopped and what needs to happen before it can pick the feature back up.

![Pixel-art crossed sword and hammer](assets/ready-to-build.png) **The feature moves again.** Another task fixes the ability IDs, then the dragon task continues from its notes. If an older task tries to update a work list that has since changed, `repo-work` asks it to catch up first.

Everything stays ordinary repository work: code, branches, worktrees, Markdown, and conversation. The plugin keeps the work list coherent and gives each implementation one canonical brief, so the task a worker receives is still the task the coordinating chat meant to send.

## Where it saves rework

You plan a change carefully, hand it to another AI, and get back something that is almost right. One half of a protocol was postponed even though both halves had to move together. The exact test commands became “run the relevant checks.” A request to read one section became a tour of half the repository. After seeing the same mistakes recur, they stop looking random: they happen when the task is retold on its way to the worker.

`repo-work` keeps that retelling out of the workflow. The worker goes back to the accepted attempt brief, while the launch message says only where to work and which checkpoint to pick up. For work that crosses components, a small contract table records who owns what, which parts must move together, and how to prove the result works. Review still gets the final say, but it should not have to reconstruct the task first.

## What it gives you

- one shared work list for features, bugs, cleanup, and foundation work;
- an inbox where any Codex task can leave something it discovered;
- a canonical attempt brief that survives the trip from planning to implementation;
- enough recorded context and evidence to pause work, resume it, and review it without reconstruction;
- early warnings for stale state, contradictory launch instructions, and incomplete cross-component checkpoints;
- readable local files instead of another hosted service or database.

It is most useful when repository work lasts for days, several Codex tasks are involved, or one piece of work keeps uncovering prerequisites. A short isolated change probably does not need it.

## Install from GitHub

The plugin currently supports macOS and Linux. It uses [uv](https://docs.astral.sh/uv/) to provide its Python 3.14 runtime and installed command.

Add this repository as a Codex marketplace, then install the plugin:

```sh
codex plugin marketplace add valsteen/codex-repo-work
codex plugin add codex-repo-work@codex-repo-work
```

Start a Codex task in the repository and ask:

> Set up Repository Work here and explain how I can use it from one chat or several chats.

When another task uncovers something worth keeping, ask it:

> Add this to the repository work inbox: saving a boss fight currently captures temporary animation state. Include what you found and why it could block phase-two save support.

In any chat, you can ask what came in, how it relates to work already underway, and what is ready to start. That chat borrows the short coordination lease only for the shared change, then releases it. For example:

> Show me where the project stands, what is waiting on something else, and what you recommend doing next.

## One chat or several

There is no master chat to keep alive. The single-chat workflow remains the simplest option: one chat can inspect the ledger, briefly borrow coordination for scheduling changes, claim its attempt, and finish the work.

Existing schema-v1 ledgers need one explicit `repo-work migrate --to v2` cutover before lease or resource commands become available. Until then, those commands return `MIGRATION_REQUIRED` rather than pretending legacy permanent ownership has lease semantics.

For concurrent work, open one chat per distinct outcome. Each chat claims only its own attempt, so unrelated item changes do not invalidate its local actions. A chat that needs to admit work, change dependencies, activate an attempt, or accept a result briefly acquires the exclusive coordination lease and releases it after that atomic change. If another chat already holds coordination, the command identifies that chat and the lease expiry so the current chat can wait or ask you about revocation.

Projects may declare host-local exclusive resources such as `bitwig-live`. A chat that owns an attempt must also claim each resource named by that item before live use. A conflict identifies the holding attempt, chat, host, and expiry. Offline work with no declared scarce resource remains available concurrently.

## Core model

Three ideas keep responsibilities clear:

- the shared work list answers what the project may work on next;
- topic folders hold research, designs, plans, and reports;
- branches and worktrees belong to the concrete attempt to implement something.

The project stores its private working state in ignored local files:

```text
.codex/work/
  authority.json
  v2/
    current.md
    queue.md                  # generated overview
    inbox/
    items/                    # authoritative lifecycle and context Markdown
    attempts/                 # briefs and renewable ownership leases
    resources/                # project-declared scarce resources
    leases/
      coordination.md         # present after first coordination lease
      resources/
    history/
.codex/topics/
```

The Markdown stays readable and easy to inspect in Finder. Each item file is authoritative; `queue.md` is regenerated as a convenient overview. `repo-work` checks that these files agree before changing them, fences expired or revoked owners, refuses updates made from an older relevant view, and prepares worker launches without copying the task semantics into another prompt. SQLite is not required and is not an authority.

The plugin contains:

- `repo-work`, a small Python command that checks and updates the shared files;
- `$repo-work`, which helps any chat explain the current picture, borrow coordination when needed, and choose what happens next;
- `$repo-work-intake`, which lets any task leave a finding for later review;
- `$bounded-implementer`, which follows one accepted brief and returns evidence for independent review.

## Runtime and development

The package targets Python 3.14 only. msgspec provides the immutable records and strict JSON decoding used at repository boundaries. `.python-version` pins the current stable 3.14 patch release. uv manages Python installation, the project environment, dependencies, the checked-in lockfile, and command execution.

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

Local checks, CI, and the plugin launcher all use the package installed by uv. The checked-in uv lockfile is the single development dependency record.

CI also validates the plugin and its skills before the repository is published.
