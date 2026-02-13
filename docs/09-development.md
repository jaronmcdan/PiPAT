# Development

## Run Tests

```bash
python -m pytest
```

Coverage behavior is configured in `pytest.ini` and `.coveragerc`.

## Editable Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

## Repo Layout

- Source code: `src/roi/`
- Deployment artifacts: `deploy/`
- Installer/build scripts: `scripts/`
- Tests: `tests/`

## Adding Dependencies

- Runtime dependency: add under `[project].dependencies` in `pyproject.toml`
- Dev-only dependency: add under `[project.optional-dependencies].dev`
