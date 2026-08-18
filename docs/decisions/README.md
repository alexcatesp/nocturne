# Architecture Decision Records

One file per decision, numbered sequentially, in the format CLAUDE.md section 5
requires: context, options considered, decision, consequences.

An ADR is written whenever SPEC.md is silent or ambiguous and a choice had to be
made anyway, and whenever a dependency is added or dropped relative to the stack
listed in SPEC.md section 4.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-executor-layering.md) | Ekos for modules, direct INDI for properties | Accepted |
| [0002](0002-async-indi-client.md) | An async INDI client in place of pyindi-client | Accepted, revised |
| [0003](0003-config-schema-location.md) | Where the Pydantic configuration models live | Accepted |
| [0004](0004-closed-vocabularies.md) | Closed enumerations where the specification lists examples | Accepted |
| [0005](0005-executor-transport-settings.md) | Executor transport timings without a fourth YAML file | Accepted |
| [0006](0006-building-from-source.md) | Building INDI, StellarSolver and KStars from source, at pinned refs | Accepted, revised twice |
| [0007](0007-m1-pointing-is-ungated.md) | M1 leaves pointing ungated, and how that is contained | Accepted (limitation) |
| [0008](0008-raspberry-pi-os-trixie.md) | Raspberry Pi OS Trixie, not Bookworm — KStars 3.8.x is Qt6/KF6 | Accepted |
| [0009](0009-werror-and-trixie-gcc.md) | INDI's forced `-Werror` is stripped before building on Trixie | Accepted |
| [0010](0010-dual-meridian-enforcement.md) | Meridian limits enforced in the governor *and* in the driver | Accepted — implement in M2 |
| [0011](0011-m1-mount-link-verified.md) | The Wave 150i runs on direct USB serial; the WiFi fallback is not taken | Accepted |
| [0012](0012-driver-reported-limits.md) | Which of the driver and the configuration is authoritative, per value | Accepted |
| [0013](0013-local-configuration-overrides.md) | Shipped configuration, and an untracked local override beside it | Accepted |
| [0014](0014-chrony-seccomp-disabled.md) | chrony's seccomp filter is disabled so it can read the PPS reference clock | Accepted |
| [0015](0015-gpsd-read-only-and-tx-unconnected.md) | Nothing writes to the GPS receiver: gpsd read-only, and the Pi's TX left unwired | Accepted |
| [0016](0016-addendum-a-placement.md) | Addendum A ships as its own document and merges into SPEC.md before M3 | Accepted |
| [0017](0017-collimation-assistant.md) | A defocused-star collimation assistant; the operator turns the screws | Accepted — implement in M4 |
