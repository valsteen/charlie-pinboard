# Reachability inventory

Use this reference for a repository-wide cleanup whose stopping condition requires accounting for production definitions. A mechanical inventory is a completeness aid and candidate generator. It cannot prove semantic reachability in a dynamic program, and a textual reference does not prove a real producer or consumer.

## Keep compact evidence

Maintain a private scratch ledger with:

- supported roots and the authority that declares them;
- dynamic mechanisms and how each is enumerated;
- inventory command or analyzer, scope, completion state, and result count;
- candidate selector, references, producer, consumer, persisted or protocol responsibility, provenance, disposition, and next observation;
- retained exceptions and their exact reopen condition.

Keep paths, symbols, counts, and short conclusions. Do not paste full source files or unfiltered analyzer output into context. Work through one coherent candidate family at a time. Re-run only affected inventories during recursion, then run the complete set once at the fixed point.

## Generic method

1. Enumerate tracked production files and package contents separately from tests, examples, generated files, and prototypes.
2. Enumerate production roots from product documentation, package metadata, executables, framework or plugin registration, supported public APIs, protocols, and persisted-data readers.
3. Enumerate definitions with the best existing language-aware tool. Record stable selectors rather than definition bodies.
4. Enumerate static references, imports, exports, registrations, and serialized names. Treat ambiguous name or attribute matches as leads to inspect.
5. Enumerate dynamic mechanisms explicitly: reflection, dependency injection, generated code, runtime registries, decorators, module hooks, configuration-selected names, plugin discovery, and deserialization.
6. Reconcile every production definition to a root, verified dynamic mechanism, compatibility responsibility, cleanup candidate, or exact retained exception. Verify each candidate's real producer and consumer before deciding its disposition.

Prefer analyzers already declared by the repository. Do not add a permanent dependency merely to perform one audit. A temporary standard-library scanner or already-available analyzer is appropriate when it replaces repeated manual searches.

## Python

Start with package metadata and the installed shape: inspect `pyproject.toml` scripts and entry points, `__main__.py`, package inclusion rules, documented imports, and framework or plugin registrations. Do not assume that every importable module is a supported library API.

Use the repository's Ruff and type checker results first. They catch unused imports, undefined names, impossible type paths, and related residue, but they do not detect all unused functions, classes, methods, enum members, or fields.

For a compact definition inventory, use a temporary standard-library `ast` scanner over production `.py` files to list module-level functions, async functions, classes, assignments that define public registries or constants, and class members when the cleanup family requires them. Pair it with import and name-reference counts, then inspect ambiguous attribute access manually. If the project already uses Vulture, run it as a candidate generator; do not accept its report without checking dynamic use and supported roots.

Account explicitly for Python mechanisms that evade ordinary reference searches, including packaging entry points, `importlib`, module or class `__getattr__`, decorators that register callables, `singledispatch`, framework discovery, serializer tags, string-named callbacks, and registries populated at import time.

Coverage or tracing from representative installed commands is useful positive evidence that a path executes. Lack of coverage is only a cleanup lead because supported paths may be input-, platform-, or state-dependent. Conversely, tests executing a symbol do not make it production-reachable.

At the fixed point, report the definition count, how many are rooted dynamically, how many are retained for compatibility, how many exact exceptions remain, and whether the final inventory produced any new candidates. Do not claim that Python reachability was mechanically proven.
