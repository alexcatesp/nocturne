"""What each device needs written to it — docs/FIELD-NOTES-M1.md sections 10-12.

:mod:`nocturne.executor.link` owns *when* configured values are applied. This
module owns *what*, per device class, and nothing else. Each function returns a
:class:`~nocturne.executor.link.DeviceLink` already carrying its steps, so the
call site says which instrument it wants and not how one is kept configured.

Property and element names come from the recorded dumps in
``backend/tests/fixtures/hardware/``, not from memory: ``CCD_CONTROLS.Gain``,
``CCD_CONTROLS.Offset``, ``FILTER_NAME.FILTER_SLOT_NAME_<n>``. Where a name here
is wrong the bring-up refuses and lists what the driver actually offers, which
is the loud failure rather than a silent one.
"""

from __future__ import annotations

import logging

from nocturne.executor.executor import Executor
from nocturne.executor.link import DeviceBringUpError, DeviceLink, Step, write_values
from nocturne.schemas.equipment import FilterWheel, Focuser, GuideCamera, ImagingCamera

logger = logging.getLogger("nocturne.executor.instruments")

#: ZWO camera gain and offset. Both cameras come up on the driver's own values.
CCD_CONTROLS = "CCD_CONTROLS"
GAIN_ELEMENT = "Gain"
OFFSET_ELEMENT = "Offset"

#: The wheel's stored slot names. Written, never read — see filter_wheel_link.
FILTER_NAME = "FILTER_NAME"
FILTER_SLOT_NAME = "FILTER_SLOT_NAME_{position}"

#: The focuser's travel limit, reported by the driver from the hardware.
FOCUS_MAX = "FOCUS_MAX"
FOCUS_MAX_VALUE = "FOCUS_MAX_VALUE"

#: Backlash. Only written when a measured value is configured.
FOCUS_BACKLASH_STEPS = "FOCUS_BACKLASH_STEPS"
FOCUS_BACKLASH_VALUE = "FOCUS_BACKLASH_VALUE"


def imaging_camera_link(
    executor: Executor, camera: ImagingCamera, *, gain_profile: str
) -> DeviceLink:
    """The imaging camera, held at the configured gain and offset.

    The 533MM ships at Gain 200, Offset 1. The gain that matters is the one in
    the active gain profile, because that is the number the exposure solver
    divides by (SPEC section 15) — a driver default of 200 against a configured
    profile of 100 means every computed exposure is wrong by a stop, and nothing
    in the frame says so.
    """
    profile = camera.gain_profile(gain_profile)
    return DeviceLink(
        executor,
        device=camera.indi_device_name,
        after_connect=(
            write_values(
                CCD_CONTROLS,
                {GAIN_ELEMENT: float(profile.gain), OFFSET_ELEMENT: float(camera.offset)},
                what=f"gain profile {profile.name!r}",
            ),
        ),
        label=camera.name,
    )


def guide_camera_link(executor: Executor, camera: GuideCamera) -> DeviceLink:
    """The guide camera, held at the configured gain and offset."""
    return DeviceLink(
        executor,
        device=camera.indi_device_name,
        after_connect=(
            write_values(
                CCD_CONTROLS,
                {GAIN_ELEMENT: float(camera.gain), OFFSET_ELEMENT: float(camera.offset)},
                what="guide camera gain and offset",
            ),
        ),
        label=camera.name,
    )


def filter_names_step(wheel: FilterWheel) -> Step:
    """Write the configured filter names into the driver, overwriting whatever is there.

    The direction is the whole point (docs/FIELD-NOTES-M1.md section 10.2). The
    EFW arrived holding ZWO factory names — Red, Green, Blue, H_Alpha, SII,
    OIII, LPR, Luminance — which match nothing in the wheel. Anything that read
    those would file B frames as H-Alpha, and no image would look wrong.

    So ``equipment.yaml`` is the source of truth and the driver is told, every
    connect. A difference is not a conflict to reconcile: configuration wins.
    It is logged, though, because a wheel someone has physically rearranged
    shows up here first and should leave a trace.
    """

    async def step(executor: Executor, device: str, *, timeout: float | None) -> None:
        prop = await executor.wait_for_property(device, FILTER_NAME, timeout=timeout)

        wanted: dict[str, str] = {}
        for position, slot in sorted(wheel.slots.items()):
            element = FILTER_SLOT_NAME.format(position=position)
            if element not in prop.elements:
                raise DeviceBringUpError(
                    f"{device}.{FILTER_NAME} has no element {element}, so slot "
                    f"{position} cannot be named. The driver offers: "
                    f"{', '.join(sorted(prop.elements))}. Check that "
                    "filter_wheel.slots in config/equipment.yaml matches the "
                    "number of positions this wheel has."
                )
            wanted[element] = slot.name if slot.name is not None else f"Empty{position}"

        differing = {
            element: (str(prop.get(element)), name)
            for element, name in wanted.items()
            if str(prop.get(element)) != name
        }
        if differing:
            logger.warning(
                "the wheel's stored filter names disagree with equipment.yaml; "
                "overwriting them from configuration, which is authoritative",
                extra={
                    "device": device,
                    "differences": {
                        element: {"driver": was, "configured": now}
                        for element, (was, now) in sorted(differing.items())
                    },
                },
            )

        # Written unconditionally, not only when they differ: a write that
        # happens sometimes is one whose failure is noticed sometimes.
        await executor.set_property(device, FILTER_NAME, dict(wanted), timeout=timeout)

    return step


def filter_wheel_link(executor: Executor, wheel: FilterWheel) -> DeviceLink:
    """The filter wheel, with its slot names written from configuration."""
    return DeviceLink(
        executor,
        device=wheel.indi_device_name,
        after_connect=(filter_names_step(wheel),),
        label="filter wheel",
    )


def focus_limit_step(focuser: Focuser) -> Step:
    """Check the configured travel limit against the driver's, and refuse a looser one.

    The driver reports ``FOCUS_MAX.FOCUS_MAX_VALUE`` from the hardware — 100000
    on the EAF — and that is authoritative. ``equipment.yaml`` previously said
    60000, a number with no provenance.

    So the configuration does not set this limit; at most it *tightens* it. An
    unset value means "the driver's". A configured value above the driver's is
    refused rather than clamped, because clamping would silently grant travel
    the operator thought they had forbidden.
    """

    async def step(executor: Executor, device: str, *, timeout: float | None) -> None:
        prop = await executor.wait_for_property(device, FOCUS_MAX, timeout=timeout)
        reported = prop.get(FOCUS_MAX_VALUE)
        if reported is None:
            raise DeviceBringUpError(
                f"{device}.{FOCUS_MAX} has no {FOCUS_MAX_VALUE}; the focuser's "
                "travel limit cannot be established."
            )
        driver_maximum = float(reported)

        if focuser.max_position is None:
            logger.info(
                "using the focuser travel limit the driver reports",
                extra={"device": device, "max_position": driver_maximum},
            )
            return
        if focuser.max_position > driver_maximum:
            raise DeviceBringUpError(
                f"focuser.max_position is {focuser.max_position}, but {device} "
                f"reports a hardware limit of {driver_maximum:.0f}. Configuration "
                "may only tighten that limit, never raise it — the driver's value "
                "comes from the hardware. Lower it or remove it."
            )
        logger.info(
            "configuration tightens the focuser travel limit",
            extra={
                "device": device,
                "driver_maximum": driver_maximum,
                "configured": focuser.max_position,
            },
        )

    return step


def focuser_link(executor: Executor, focuser: Focuser) -> DeviceLink:
    """The focuser.

    Backlash is written only when a *measured* value is configured. The EAF
    ships 180 with compensation disabled; 180 is a ZWO default and writing an
    unmeasured number over it would replace one guess with another
    (docs/FIELD-NOTES-M1.md section 12). Until M2 measures it, the driver keeps
    what it has and nothing pretends otherwise.
    """
    steps: list[Step] = [focus_limit_step(focuser)]
    if focuser.backlash_steps is not None:
        steps.append(
            write_values(
                FOCUS_BACKLASH_STEPS,
                {FOCUS_BACKLASH_VALUE: float(focuser.backlash_steps)},
                what="measured backlash",
            )
        )
    return DeviceLink(
        executor,
        device=focuser.indi_device_name,
        after_connect=tuple(steps),
        label="focuser",
    )


__all__ = [
    "CCD_CONTROLS",
    "FILTER_NAME",
    "FILTER_SLOT_NAME",
    "FOCUS_BACKLASH_STEPS",
    "FOCUS_MAX",
    "GAIN_ELEMENT",
    "OFFSET_ELEMENT",
    "filter_names_step",
    "filter_wheel_link",
    "focus_limit_step",
    "focuser_link",
    "guide_camera_link",
    "imaging_camera_link",
]
