# Repository Guidelines

## Project Structure & Module Organization

Source code lives under `src/feishu_shadow_agent/`. Core daemon orchestration is in `daemon.py`, ingestion and normalization in `ingestion.py`, task routing in `routing.py`, Hermes integration in `hermes.py` and `processing.py`, Feishu CLI access in `feishu/lark_cli.py`, and SQLite persistence in `store/sqlite_store.py` plus `store/migrations/`. Tests live in `tests/` with fixtures in `tests/fixtures/`. Product specs and operational notes are under `docs/specs/`, `docs/plans/`, and `docs/testing.md`; generated cover assets live in `docs/assets/covers/`.

## Build, Test, and Development Commands

Create a local environment and install dev dependencies:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run all local tests with `python -m pytest -q`. Run Ruff checks with `python -m ruff check .` and `python -m ruff format --check .`; use `pre-commit run --all-files` to exercise the same Ruff lint-autofix and formatter hooks as git commits. Frontend Operator Console changes must pass `npm --prefix frontend/operator-console run build`, which also refreshes the bundled static assets under `src/feishu_shadow_agent/console_static/`. Release or packaging changes should also run `python -m build` and inspect the wheel for `feishu_shadow_agent/console_static/index.html` plus referenced assets. Run `git diff --check` before handing off changes to catch whitespace issues. For local operation, copy `config.example.yaml` to `config.yaml`, run `python -m feishu_shadow_agent policy import-config --config config.yaml`, then run `python -m feishu_shadow_agent doctor --config config.yaml` before starting `python -m feishu_shadow_agent daemon --config config.yaml --dry-run`.

## Coding Style & Naming Conventions

Use Python 3.11+ and keep the existing typed, dataclass/Pydantic style. Prefer small functions with explicit return types when practical. Use snake_case for modules, functions, variables, config keys, and test names; use PascalCase for classes. Keep comments sparse and explain intent, safety boundaries, or non-obvious Feishu/Hermes behavior rather than restating code.

## Testing Guidelines

Tests use pytest and should remain side-effect free by default: fake Feishu/Hermes clients are preferred over real network calls. Name tests `test_<behavior>.py` or `test_<specific_case>` and place them near the relevant existing test module. End-to-end checks against real Feishu credentials are opt-in and documented in `docs/testing.md`; use dry-run first.

## Commit & Pull Request Guidelines

Recent history uses imperative summaries such as `Implement retention pruning` and scoped fixes like `fix: harden p4 dispatch idempotency`. Keep commits focused and describe behavior, not mechanics. PRs should include the problem, the change, validation commands, and any config or operational impact. Include screenshots only for asset or README visual changes.

## Security & Configuration Tips

Never commit `config.yaml`, `data/`, `logs/`, tokens, keychain material, or downloaded resources. Keep real sends gated by `doctor`, dry-run validation, approval queue behavior, and per-chat policy.
