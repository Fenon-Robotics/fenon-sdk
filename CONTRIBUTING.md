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
uv run python scripts/check_no_comments.py   # no inline comments
uv run python scripts/generate_types.py --check
uv run pytest                                # tests
```

## How the code is organised

```
src/fenon/
  _client.py      Fenon: the public client
  _core/          core services shared by every model (HTTP, errors, logging, uploads, runs, usage)
  models/         one package per capability, e.g. models/pose/ with base.py and one folder per model contract
```

`docs/CODEBASE.md` lists every file, what owns it and what it may import. `docs/features/` explains each area. `docs/specs/` holds the build specs. `docs/decisions.md` records why things are the way they are.

- **Dependencies point one way.** `models` imports `_core`; `_core` never imports `models`; `_client.py` imports both. A test enforces it.
- **Deep modules.** Each module exposes a small, deliberate interface and keeps its details private. Prefer a few substantial modules over many thin ones. No `utils`, `helpers` or `common` folders: decide which module owns the code.
- **Private by default.** Anything not exported from `fenon/__init__.py` is internal. Internal modules and names start with `_`.
- **Adding a model.** A model that implements an existing contract needs no code. A new contract under an existing capability adds a folder next to `models/pose/hamer/`. A new capability adds `models/<capability>/` and one attribute on `Fenon`.

## Code rules

1. **No inline comments.** No `#` comments anywhere in `src/`, `scripts/` or `tests/`. Explain intent with names, structure and docstrings; put rationale in `docs/`.
2. **Docstrings are minimal.** One line at most, saying what the module, class or function is or does. No `Args:`, `Returns:`, `Raises:` or `Attributes:` sections and no extra paragraphs. Where the name already says it, leave the docstring out. Behaviour and rationale belong in `docs/features/` and `docs/decisions.md`.
3. **No patch work.** Fix the cause in the module that owns it. No workarounds, no dead code, no heuristics standing in for a real signal, no code that only exists to make a test pass.
4. **Typed everywhere.** `pyright` runs in strict mode on `src/`. No `Any` in public signatures.
5. **Errors.** Raise the classes in `fenon._core.errors`; never let `httpx2` exceptions or raw status codes reach users. Never put an API key, request body, file path or storage URL in an error message or log record.
6. **Generated code is never edited by hand.** Change the contract in `docs/`, then run `scripts/generate_types.py`.
7. **Standard library and existing dependencies first.** Adding a runtime dependency needs a decision-log entry.

## Trying the SDK without the real API

`scripts/dev_api.py` simulates every v1 endpoint on your machine. It is a development tool, never packaged:

```sh
uv run python scripts/dev_api.py            # prints the key and base URL to use
FENON_API_KEY=fenon_dev_local_simulation_key_000 FENON_BASE_URL=http://127.0.0.1:8787/v1 uv run python your_script.py
```

Runs move through every status in about 4 seconds and return 90 synthetic frames. Upload a file named `fail*.mp4` to see a failed run. Add `--first-cut` to simulate the planned first `fenon-api` release: `GET /v1/models` works and every other endpoint answers `501 not_implemented`.

## Tests

- Every behaviour has a test; every bug fix adds the test that would have caught it.
- Unit tests talk to the network only through `httpx2.MockTransport`.
- End-to-end tests (`tests/e2e/`) use real sockets against the scripted local server in `tests/_wire.py`, bound to `127.0.0.1`. No test reaches an external host.
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
- A second reviewer is required for changes to `docs/openapi.yaml` or `docs/contracts/`, to API-key or storage-URL handling, to retry or idempotency rules, or to logging.
