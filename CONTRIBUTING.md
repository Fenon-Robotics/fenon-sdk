# Contributing to the Fenon Python SDK

This package is production code that customers install. The rules below keep it small, predictable and easy to pick up. Read them before your first change.

## Set up

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # creates .venv with the package and the dev tools
```

## Checks

Run all of these before opening a pull request. CI runs the same commands on Python 3.11–3.14 and on Windows.

```bash
uv run ruff check .                          # lint
uv run ruff format --check .                 # formatting
uv run pyright                               # types (strict)
uv run python scripts/check_comment_rules.py # no comments; one docstring per file
uv run python scripts/generate_types.py --check   # generated types match the pinned contract
uv run pytest                                # tests
```

## How the code is organised

```
src/fenon/
  _client.py      Fenon: the public client
  _core/          core services shared by every model (HTTP, errors, logging, configuration, uploads, runs, contracts)
  _live/          live sessions over WebRTC (switched off in client.pose.hand for now)
  models/         one package per capability, e.g. models/pose/ with base.py and one folder per model contract
```

`docs/CODEBASE.md` lists every file, what owns it and what it may import. `docs/features/` explains each area. `docs/specs/` holds the build specs. `docs/decisions.md` records why things are the way they are.

- **Dependencies point one way.** `_core` ← `_live` ← `models` ← `_client.py`: `_core` imports nothing above it, `_live` imports only `_core`, and `models` never imports the client. A test enforces it.
- **Packages are used through their interface.** `_core` and `_live` export what other packages need from their `__init__.py`; nothing outside a package imports its modules. A test enforces it.
- **Settings and configuration stay apart.** What a user may set is `_core/settings.py` (`FENON_*`). Fixed values (timeouts, retries, limits) live in their package's `config.py`. No other module holds a tunable of its own.
- **Deep modules.** Each module exposes a small, deliberate interface and keeps its details private. Prefer a few substantial modules over many thin ones. No `utils`, `helpers` or `common` folders: decide which module owns the code.
- **Private by default.** Anything not exported from `fenon/__init__.py` is internal. Internal modules and names start with `_`.
- **Adding a model.** A model that implements an existing contract needs no code. A new contract under an existing capability adds a folder next to `models/pose/hamer/`. A new capability adds `models/<capability>/` and one attribute on `Fenon`.

## Code rules

1. **No comments.** No `#` comments anywhere in `src/`, `scripts/` or `tests/`. Explain intent with names and structure; put rationale in `docs/`.
2. **One docstring per file.** Each file has exactly one docstring, at the top: what the module owns, its contract, the non-obvious rules and the doc they follow; short but complete. No docstrings on classes, functions or methods, generated files included. What users need to know is in `README.md` and `docs/features/`; rationale in `docs/decisions.md`. `scripts/check_comment_rules.py` enforces rules 1 and 2.
3. **No patch work.** Fix the cause in the module that owns it. No workarounds, no dead code, no heuristics standing in for a real signal, no code that only exists to make a test pass.
4. **Typed everywhere.** `pyright` runs in strict mode on `src/`. No `Any` in public signatures.
5. **Errors.** Raise the error classes `fenon._core` exports; never let `httpx2` exceptions or raw status codes reach users. Never put an API key, request body, file path or storage URL in an error message or log record.
6. **Contracts come from fenon-api; generated code is never edited by hand.** fenon-api owns the contract. Pin a version with `uv run python scripts/sync_contracts.py <path to fenon-api>`, which copies it into `docs/contracts/fenon-api/<version>/` and regenerates the types. Never edit the pinned files or the generated code.
7. **Standard library and existing dependencies first.** Adding a runtime dependency needs a decision-log entry.

## Trying the SDK against a local fenon-api

Run fenon-api from fenon-app (its `README.md`): the API, its Postgres, and for recorded runs the R2 settings, the dispatcher and a model engine (fenon-api's `scripts/fake_scala.py` stands in for SCALA without a GPU). Then point the SDK at it:

```sh
FENON_API_KEY=<a key of the local tenant> FENON_BASE_URL=http://127.0.0.1:8000/v1 uv run python your_script.py
```

## Tests

- Every behaviour has a test; every bug fix adds the test that would have caught it.
- Unit tests talk to the network only through `httpx2.MockTransport`.
- End-to-end tests (`tests/e2e/`) use real sockets against the scripted local server in `tests/_wire.py`, bound to `127.0.0.1`. No test in the default suite reaches an external host.
- Tests in `tests/live_stack/` are opt-in: they run only with `FENON_LIVE_STACK_API_KEY` set, against fenon-app's local stack (fenon-api and the echo-engine gateway), and reach the STUN server the local fenon-api hands out. See `docs/features/live.md`.
- Time and randomness are injected, never patched globally.
- Security boundaries (the API key, storage URLs, logging) get adversarial tests.

## Docs and decisions, in the same pull request

Every change updates, in the same pull request:

- `docs/CODEBASE.md`, for added, moved or removed files;
- the relevant `docs/features/*.md`;
- `docs/decisions.md`, for any durable decision (`## [date] title` with Decision, Why, Rejected alternatives, Risks);
- the spec in `docs/specs/` if the build deviates from it.

Specs come before code: a feature or refactor that touches more than one module gets a spec, approved before implementation.

## Pull requests

- One spec slice or one fix per pull request.
- A second reviewer is required for changes to `docs/contracts/` (re-pinning fenon-api's contract), to API-key or storage-URL handling, to retry or idempotency rules, or to logging.
