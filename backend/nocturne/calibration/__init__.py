"""Calibration — SPEC section 6.

The darks and flats library of SPEC section 10.2 arrives with M3. What is here
now is the meridian calibration of SPEC section 9.1: which measured profile
applies to the rig as it currently stands (ADR 0021).
"""

from __future__ import annotations

from .meridian import resolve_measured_band

__all__ = ["resolve_measured_band"]
