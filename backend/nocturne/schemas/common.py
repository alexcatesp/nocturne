"""Shared base model for every Nocturne configuration schema.

SPEC section 5: all configuration is YAML, loaded and validated by Pydantic
models at startup, and startup fails loudly on invalid config.

Two properties are enforced here for every configuration model:

``extra="forbid"``
    A typo in a YAML key is a hard error, not a silently ignored line. A
    misspelled ``altitude_min_deg`` that fell back to a default would be a
    safety failure.

``frozen=True``
    Configuration is immutable once loaded. CLAUDE.md invariant 2 requires that
    the agent cannot modify limits at runtime; making the models frozen means
    there is no code path that can, whatever the caller.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

#: Arcseconds in one radian. Used to derive plate scales from pixel size and
#: focal length. A unit conversion, not a tunable threshold.
ARCSEC_PER_RADIAN = 206_264.806_247_096_36

#: Arcseconds in one arcminute.
ARCSEC_PER_ARCMIN = 60.0


class StrictModel(BaseModel):
    """Base class for configuration models: strict, immutable, no extras."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
    )
