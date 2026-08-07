"""Shipped configuration plus an untracked local override — SPEC section 5.

The repository ships `config/equipment.yaml` with a placeholder site, and the
operator has to put their real coordinates in it. Those two facts are in direct
conflict: the file is version-controlled *and* it is the file that must be
edited, so every `git pull` collides with the edit that makes the rig work. The
operator reported it after M1; nothing in the test suite could have found it,
because it is not a property of the code.

So each shipped file may be accompanied by an untracked neighbour —
`equipment.local.yaml` beside `equipment.yaml` — which is merged **over** it at
load time. The shipped file stays a working placeholder that is never edited;
the local file holds the handful of values that are true of one site and no
other, and git never sees it.

Three properties matter, and each is a test:

**Mappings merge, everything else replaces.** A local file that sets
`site.latitude` leaves `site.longitude` alone. A local file that sets
`filter_wheel.slots` replaces the whole list — a merged list would have to guess
whether the operator meant to add a filter or renumber the wheel, and guessing
about which filter is in slot 4 is how B frames get filed as H-Alpha
(ADR 0012).

**Every value knows where it came from.** `check-config` prints the provenance,
because a configuration assembled from two files is one where "I changed that"
and "the change took effect" are different statements. :func:`merge_documents`
returns both the merged document and a map from dotted key path to the file that
last set it.

**Nothing here writes anything.** The local file is created by the operator, by
hand, once. Nocturne reads it and never generates, repairs or updates it — same
rule as the shipped files, and enforced by the same structural test
(CLAUDE.md invariant 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

#: The suffix an override file carries: `safety.yaml` -> `safety.local.yaml`.
LOCAL_SUFFIX: Final = ".local.yaml"

#: What a shipped configuration file ends with.
SHIPPED_SUFFIX: Final = ".yaml"

#: Separator for the dotted paths used to report provenance.
PATH_SEPARATOR: Final = "."


def local_path_for(path: Path) -> Path:
    """The untracked override that would sit beside ``path``.

    ``config/equipment.yaml`` -> ``config/equipment.local.yaml``. Deriving it
    rather than listing the three names means a fourth configuration file gets
    an override for free, and cannot be forgotten.
    """
    stem = path.name
    if stem.endswith(LOCAL_SUFFIX):
        raise ValueError(f"{path} is already a local override file")
    if not stem.endswith(SHIPPED_SUFFIX):
        raise ValueError(f"{path} is not a .yaml file, so it has no local override")
    return path.with_name(stem[: -len(SHIPPED_SUFFIX)] + LOCAL_SUFFIX)


def is_local_path(path: Path) -> bool:
    """Whether ``path`` names an untracked override rather than a shipped file."""
    return path.name.endswith(LOCAL_SUFFIX)


@final
@dataclass(frozen=True, slots=True)
class Override:
    """One value the local file set, and what it displaced.

    ``previous`` is ``None`` when the key was absent from the shipped file
    entirely — which is different from it having been present and null, and the
    report says so.
    """

    key: str
    value: object
    previous: object | None
    was_present: bool


@final
@dataclass(frozen=True, slots=True)
class MergedDocument:
    """A merged configuration document and the provenance of every leaf."""

    document: dict[str, object]
    #: Dotted key path -> the file that last set it. Every leaf appears here.
    sources: dict[str, Path]
    #: Only what the local file changed, in the order the local file gave them.
    overrides: tuple[Override, ...]

    def source_of(self, key: str) -> Path | None:
        """Which file set ``key``, or ``None`` if nothing did."""
        return self.sources.get(key)


def merge_documents(
    base: dict[str, object],
    base_path: Path,
    local: dict[str, object] | None,
    local_path: Path | None,
) -> MergedDocument:
    """Merge ``local`` over ``base``, recording where each value came from.

    Mappings are merged key by key, recursively. Anything else — scalar, list,
    null — replaces wholesale. A local value that happens to equal the shipped
    one is still recorded as coming from the local file: it was written there
    deliberately, and a report that hid it would make the file look inert.

    ``local`` of ``None`` means no override file exists, which is the normal
    case in CI and on a fresh clone.
    """
    if local is None or local_path is None:
        merged = _copy_mapping(base)
        return MergedDocument(
            document=merged,
            sources=_leaf_sources(merged, base_path, prefix=""),
            overrides=(),
        )

    sources = _leaf_sources(base, base_path, prefix="")
    overrides: list[Override] = []
    merged = _merge(base, local, local_path, sources, overrides, prefix="")
    return MergedDocument(document=merged, sources=sources, overrides=tuple(overrides))


def _merge(
    base: dict[str, object],
    local: dict[str, object],
    local_path: Path,
    sources: dict[str, Path],
    overrides: list[Override],
    *,
    prefix: str,
) -> dict[str, object]:
    merged: dict[str, object] = _copy_mapping(base)
    for key, local_value in local.items():
        dotted = f"{prefix}{key}"
        base_value = base.get(key)
        was_present = key in base

        if isinstance(local_value, dict) and isinstance(base_value, dict):
            merged[key] = _merge(
                base_value,
                local_value,
                local_path,
                sources,
                overrides,
                prefix=f"{dotted}{PATH_SEPARATOR}",
            )
            continue

        # A replacement, so every leaf the old value contributed is gone.
        for stale in _leaf_paths(base_value, prefix=dotted) if was_present else ():
            sources.pop(stale, None)
        merged[key] = local_value
        sources.update(_leaf_sources({key: local_value}, local_path, prefix=prefix))
        overrides.append(
            Override(
                key=dotted,
                value=local_value,
                previous=base_value if was_present else None,
                was_present=was_present,
            )
        )
    return merged


def _copy_mapping(document: dict[str, object]) -> dict[str, object]:
    """A deep copy of the mappings, leaving leaves shared.

    Only the mappings are rebuilt: they are what the merge mutates. Lists and
    scalars are replaced wholesale and never edited in place, so sharing them is
    safe and keeps the copy cheap.
    """
    return {
        key: _copy_mapping(value) if isinstance(value, dict) else value
        for key, value in document.items()
    }


def _leaf_sources(document: dict[str, object], path: Path, *, prefix: str) -> dict[str, Path]:
    """``{dotted key: path}`` for every leaf in ``document``."""
    found: dict[str, Path] = {}
    for key, value in document.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            found.update(_leaf_sources(value, path, prefix=f"{dotted}{PATH_SEPARATOR}"))
        else:
            found[dotted] = path
    return found


def _leaf_paths(value: object, *, prefix: str) -> list[str]:
    """Every dotted leaf path ``value`` would contribute under ``prefix``."""
    if not isinstance(value, dict) or not value:
        return [prefix]
    found: list[str] = []
    for key, item in value.items():
        found.extend(_leaf_paths(item, prefix=f"{prefix}{PATH_SEPARATOR}{key}"))
    return found


def source_for_error(sources: dict[str, Path], location: tuple[object, ...]) -> Path | None:
    """The file responsible for a Pydantic error at ``location``.

    Validation runs on the merged document, so an error names a key without
    saying which file supplied it — and "invalid configuration" pointing at the
    shipped file when the operator's own override is at fault sends them to the
    wrong place entirely. Walks up from the exact location, because an error can
    be reported against a mapping whose individual leaves are what is recorded.
    """
    parts = [str(part) for part in location]
    while parts:
        found = sources.get(PATH_SEPARATOR.join(parts))
        if found is not None:
            return found
        parts.pop()
    return None
