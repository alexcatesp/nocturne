# Architecture Decision Records

One file per decision, numbered sequentially, in the format CLAUDE.md section 5
requires: context, options considered, decision, consequences.

An ADR is written whenever SPEC.md is silent or ambiguous and a choice had to be
made anyway, and whenever a dependency is added or dropped relative to the stack
listed in SPEC.md section 4.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-executor-layering.md) | Ekos for modules, direct INDI for properties | Accepted |
| [0002](0002-async-indi-client.md) | An async INDI client in place of pyindi-client | Accepted |
| [0003](0003-config-schema-location.md) | Where the Pydantic configuration models live | Accepted |
| [0004](0004-closed-vocabularies.md) | Closed enumerations where the specification lists examples | Accepted |
| [0005](0005-executor-transport-settings.md) | Executor transport timings without a fourth YAML file | Accepted |
