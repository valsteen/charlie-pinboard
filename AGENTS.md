# Repository Guidance

- Use Python 3.14.x syntax only. Keep `.python-version`, `requires-python`, Ruff `target-version`, Pyrefly, CI, and the uv lock aligned with the current stable 3.14 patch release.
- Use uv as the only dependency, environment, build, and installed-package entry point. Keep `uv.lock` checked in.
- Do not inject `PYTHONPATH` or another import path, invoke an ambient Python interpreter, add requirements files, or run source files as a substitute for the installed package.
- Keep Ruff `UP` as the only pyupgrade authority. Do not add standalone pyupgrade, MyPy, Pyright, or redundant type-checking layers.
- Keep Pyrefly strict. Convert JSON and Markdown boundary values into exact validated types before domain use.
- Keep Python types concrete. Do not use `object` or `Any` as annotation escape hatches. Use plain `Enum`; do not use `StrEnum`.
- Tests must exercise observable behavior, rejection contracts, concurrency, recovery, or package/plugin integration rather than implementation constants.
- Support macOS and Linux. Do not add cross-platform-looking branches without contention and filesystem evidence on the claimed platform.
- Preserve the observable transaction contract: a failed transition leaves the previous valid ledger intact, and concurrent stale actions are rejected.
- Keep `README.md` human-facing and descriptive. Put imperative agent and contributor constraints here.
