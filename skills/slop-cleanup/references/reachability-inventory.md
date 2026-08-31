# Reachability inventory

Use this reference for a repository-wide cleanup whose stopping condition requires accounting for production definitions. A mechanical inventory is a completeness aid and candidate generator. It cannot prove semantic reachability in a dynamic program, and a textual reference does not prove a real producer or consumer.

## Keep compact evidence

Maintain a private scratch ledger with:

- supported roots and the authority that declares them;
- dynamic mechanisms and how each is enumerated;
- inventory command or analyzer, scope, completion state, and result count;
- closed-family inventory, member count, producer/consumer disposition, and unresolved members;
- candidate selector, references, producer, consumer, persisted or protocol responsibility, provenance, disposition, and next observation;
- retained exceptions and their exact reopen condition.

Keep paths, symbols, counts, and short conclusions. Do not paste full source files or unfiltered analyzer output into context. Work through one coherent candidate family at a time. Re-run only affected inventories during recursion, then run the complete set once at the fixed point.

## Run the bundled inventory

Resolve `../scripts/inventory.py` relative to this reference and run it with the Python interpreter provided by the plugin host. Give it the repository and exact production and test roots. Keep its JSON in private scratch space; inspect `coverage`, `summary`, and `candidates` before loading individual declaration or family records.

For a repository containing production Python, run the same tracked revision and roots twice:

```text
python inventory.py --repository <repo> --production-root <root> --test-root <root> --mode generic
python inventory.py --repository <repo> --production-root <root> --test-root <root> --mode python-ast
```

Require the two reports to have the same `input_digest`. Use the generic run as the portability baseline and the AST run as additional Python evidence. Compare candidate selectors and closed-family atoms in both directions. Investigate useful generic findings that disappear under enrichment, and record which additional candidates depend on AST precision. Do not silently replace the generic run with the richer one.

The generic pass deliberately uses conservative text structure rather than a Kotlin, Rust, TypeScript, or SQL parser. It inventories tracked and untracked non-ignored working-tree text and assets, declaration-like symbols in Python, Kotlin, Rust, and TypeScript, lexical reference direction, common enum shapes, SQLite `CREATE` objects, and literal `CHECK ... IN (...)` vocabularies even when SQL is embedded in another source file. Pair it with analyzers already declared by the repository. Add no parser or permanent dependency merely to make another language resemble Python.

Read every coverage receipt. `complete` means the named mechanical category completed for the given inputs, `partial` means every matching file was visited but the extraction is heuristic, and `unsupported` leaves a mandatory semantic or repository-specific pass. The helper intentionally leaves producer/consumer meaning, dynamic reachability, repeated traversals, duplicated dispatch, protocol overlap, and developer navigation unsupported. A run cannot establish the cleanup fixed point while a required unsupported or partial category lacks a separately recorded inspection or analyzer.

## Generic method

1. Enumerate tracked production files and package contents separately from tests, examples, generated files, and prototypes.
2. Enumerate production roots from product documentation, package metadata, executables, framework or plugin registration, supported public APIs, protocols, and persisted-data readers.
3. Enumerate definitions with the best existing language-aware tool. Use the bundled generic inventory as a cross-language baseline rather than as a parser-equivalent proof. Record stable selectors rather than definition bodies.
4. Inventory the atoms of each closed production family separately from definitions: enum members, tagged-union variants, command routes, serializer tags, schema alternatives, error codes, and other finite vocabularies. For each atom, find a real production producer and consumer or record its exact boundary, persistence, compatibility, or retained-exception reason. A reference to the containing type does not account for all of its members.
5. Enumerate static references, imports, exports, registrations, and serialized names. Treat ambiguous name or attribute matches as leads to inspect.
6. Enumerate dynamic mechanisms explicitly: reflection, dependency injection, generated code, runtime registries, decorators, module hooks, configuration-selected names, plugin discovery, and deserialization.
7. Reconcile every production definition and every closed-family atom to a root, verified dynamic mechanism, compatibility responsibility, cleanup candidate, or exact retained exception. Verify each candidate's real producer and consumer before deciding its disposition.

Prefer analyzers already declared by the repository. Do not add a permanent dependency merely to perform one audit. A temporary standard-library scanner or already-available analyzer is appropriate when it replaces repeated manual searches.

## Python

Start with package metadata and the installed shape: inspect `pyproject.toml` scripts and entry points, `__main__.py`, package inclusion rules, documented imports, and framework or plugin registrations. Do not assume that every importable module is a supported library API.

Use the repository's Ruff and type checker results first. They catch unused imports, undefined names, impossible type paths, and related residue, but they do not detect all unused functions, classes, methods, enum members, or fields.

For a compact definition inventory, use a temporary standard-library `ast` scanner over production `.py` files to list module-level functions, async functions, classes, assignments that define public registries or constants, and class members when the cleanup family requires them. Pair it with import and name-reference counts, then inspect ambiguous attribute access manually. If the project already uses Vulture, run it as a candidate generator; do not accept its report without checking dynamic use and supported roots.

Run a separate AST-backed closed-family inventory. Enumerate every member of `Enum` classes and every alternative of explicit union aliases; add serializer tags, command routes, schema `CHECK` alternatives, and error-code vocabularies from their owning declarations when they are not represented by those Python forms. Compare atom-level production references and boundary appearances, then inspect low-reference members for producers, consumers, and compatibility responsibility. Do not infer that all members are live because the enum or union type itself is referenced, and do not infer that a low textual count is dead when decoding, persistence, presentation, or generated dispatch consumes it indirectly.

Account explicitly for Python mechanisms that evade ordinary reference searches, including packaging entry points, `importlib`, module or class `__getattr__`, decorators that register callables, `singledispatch`, framework discovery, serializer tags, string-named callbacks, and registries populated at import time.

Coverage or tracing from representative installed commands is useful positive evidence that a path executes. Lack of coverage is only a cleanup lead because supported paths may be input-, platform-, or state-dependent. Conversely, tests executing a symbol do not make it production-reachable.

At the fixed point, report the definition count and closed-family atom count separately, how many are rooted dynamically, how many are retained for compatibility, how many exact exceptions remain, and whether the final inventory produced any new candidates. Do not claim that Python reachability was mechanically proven.
