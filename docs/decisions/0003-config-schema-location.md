# 0003 — Where the Pydantic configuration models live

Status: Accepted · 2026-08-05 · Milestone M1

## Context

SPEC section 6 lays out the repository, and puts the Pydantic configuration
models under `config/schemas/`, alongside the YAML files they validate, rather
than under `backend/nocturne/`.

Taken literally that creates a Python package outside the backend package. It
would either need a top-level `config` package — which shadows a very common
module name — or packaging machinery to map it into the `nocturne` namespace,
which then has to be repeated for setuptools, mypy and pytest, each of which
resolves modules differently.

## Options considered

1. **A top-level `config` package.** Matches the layout exactly. Risks shadowing
   and reads oddly as an import: `from config.schemas import SafetyConfig`.
2. **`package-dir` mapping `nocturne.schemas` to `config/schemas`.** Matches the
   layout and keeps the import clean, but mypy has no equivalent of setuptools'
   `package-dir`, so type checking silently stops covering the safety-critical
   configuration models.
3. **Models in `backend/nocturne/schemas/`, with `config/schemas` a symlink.**
   The documented path exists and resolves to the models; packaging, imports and
   type checking are all conventional.

## Decision

Option 3. The files live at `backend/nocturne/schemas/` and `config/schemas` is a
symbolic link to that directory. `ruff` and `mypy` are pointed at the real path
so nothing is checked twice.

## Consequences

- SPEC section 6's layout holds: `config/schemas/` contains the Pydantic models.
- Imports are `from nocturne.schemas import ...`, and mypy strict covers them.
- The repository requires a filesystem with symbolic links. Both the development
  environment and Raspberry Pi OS are Linux; Windows is not a target.
