# Development

## Run tests

```bash
python -m pytest
```

Coverage is enforced via `pytest.ini` and `.coveragerc`.

## Editable install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Repo style

- Source code lives under `src/roi/` (src-layout).
- Raspberry Pi deployment artifacts live under `deploy/`.
- Setup scripts live under `scripts/`.

## Adding a dependency

Update `pyproject.toml` under `[project].dependencies`.

If the dependency is only used for development, add it under `[project.optional-dependencies].dev`.
