# Contributing to Memstem

Thanks for your interest! Memstem is in pre-alpha — APIs, schemas, and architecture are still settling. Contributions are welcome but expect change.

## Development setup

```bash
git clone https://github.com/bbesner/memstem.git
cd memstem
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Running tests

```bash
pytest
```

## Running the linter

```bash
ruff check .
ruff format .
mypy src/
```

## Workflow

1. Create a branch from `main`: `git checkout -b feature/my-thing`
2. Make changes; ensure tests + lint pass
3. Open a PR against `main`
4. CI must pass before merge

## Commit messages

Conventional commits style:

- `feat: add Codex adapter`
- `fix: handle empty session JSONL`
- `docs: update frontmatter spec`
- `refactor: extract embedding interface`
- `test: cover RRF edge cases`

## Architecture decisions

Significant changes that touch storage layout, search ranking, or the adapter interface need an ADR in `docs/decisions/`. Number sequentially. See existing ADRs for format.

## Adding a new adapter

See [docs/adapters/adding-an-adapter.md](./docs/adapters/adding-an-adapter.md) (forthcoming) for the full guide. In short: subclass `Adapter` in `src/memstem/adapters/base.py`, implement `watch()` and `reconcile()`, register in the adapter registry.

## Code of conduct

Be kind. Disagree on ideas, not people. Assume good faith.
