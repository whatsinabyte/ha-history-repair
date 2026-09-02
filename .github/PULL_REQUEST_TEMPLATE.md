## What this PR does

A clear description of the change and why it is needed. Reference any related issue with `Fixes #123` or `Closes #123`.

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Refactor (no behaviour change)
- [ ] Other — describe:

## Checklist

- [ ] Tests pass — `pytest -q` (unit) and, if this touches a database adapter, the relevant integration suite — see [CONTRIBUTING.md](CONTRIBUTING.md)
- [ ] Ruff check clean — `ruff check app/ tests/`
- [ ] Ruff format clean — `ruff format --check app/ tests/`
- [ ] Mypy clean — `mypy app/`
- [ ] Vulture clean — `vulture app/ --min-confidence 80`
- [ ] Bandit clean — `bandit -r app/`
- [ ] `CHANGELOG.md` updated under `[Unreleased]`
- [ ] New behaviour is covered by tests
- [ ] Module boundaries respected — see [ARCHITECTURE.md](ARCHITECTURE.md) (no SQL outside a concrete adapter, `hr_models.py`/`hr_db.py`/`hr_corrections.py` stay pure)

## Testing notes

How was this tested? Include which recorder backend(s) — SQLite, MariaDB, PostgreSQL — if the change touches database code.

## Screenshots or log output

If relevant — before/after UI screenshots, log output, exported CSV.
