# MPCC focus test — does the `mpcc` train reach focus?

**What this is:** checking, in daylight and before any night, that the 200PDS
with the Baader MPCC Mark III fitted still reaches focus on stars with the
current focuser and EAF. The same session measures the EAF step size, which is
`null` in `equipment.yaml` today. About an hour, outdoors, with the camera
on the laptop or the Pi.

**Why it matters:** a coma corrector moves the focal plane. If the drawtube
cannot rack in far enough, the train never focuses on stars. That is a
spacer or hardware problem, and it is far cheaper to find in daylight than at
the start of a clear night.

> **Nocturne does not drive anything in this procedure.** Until M2 gates
> pointing, the rig is not aimed at anything under Nocturne's control (ADR
> 0007). Aim by moving the tripod, or with the mount's own app. The camera and
> the EAF may be run from KStars/Ekos or ASIStudio by hand.

> ⚠️ **The Sun.** An open Newtonian pointed near the Sun destroys the camera
> in seconds and blinds anyone looking through the finder. Cap the finder and
> the guide scope. Choose a target **well away from the Sun** and keep it that
> way for the whole test: the Sun moves about 15° an hour.

---

## Before you start

**Fitted:** MPCC → spacers → EFW → ASI533MM Pro, with the L filter in the beam.

**Backfocus.** The MPCC Mark III wants **55 mm** from its reference face to the
sensor. With ZWO's catalogue figures, which you should confirm on your own
units' datasheets:

| Part | Optical length |
|---|---|
| ASI533MM Pro (flange to sensor) | 17.5 mm |
| EFW 8 × 1.25″ | 20 mm |
| Spacers | **≈ 17.5 mm** |
| Filter glass, thickness *t* | adds ≈ *t* / 3 |

A filter lengthens the optical path by about a third of its thickness. With
filters of 1–2 mm, that is 0.3–0.7 mm. Write down exactly which spacers are
fitted. That stack is now part of the `mpcc` train, and it must be the same
the night flats are taken.

**Target:** something sharp and high-contrast, **at least 1 km away**: a mast,
a church tower, a chimney against the sky. Measure the distance on a map. You
need it below.

---

## 1. Which way is "in"

In Ekos (focus module, manual buttons) or ASIStudio, move the EAF by +5000 steps
and watch the drawtube.

- Record whether **increasing** steps moves the drawtube **out** (away from the
  tube) or **in**.
- Record the current position and the driver's maximum (`FOCUS_MAX`, 100000 on
  the reference rig, FIELD-NOTES-M1 §12).

## 2. Focus on the target

Live view, short exposures, L filter. Bring the target to its sharpest. Always
approach from the **same direction**, the last move outward then in, or the
reverse, so that backlash does not sit inside the reading.

- Record **P_target**, the EAF position at best focus.

If no position gives focus, stop here and go to **If it does not reach**.

## 3. EAF step size

With a caliper, measure the drawtube's protrusion at two positions far apart,
for example P_target and P_target − 20000, both reached from the same
direction.

```
step_size_um = (difference in mm × 1000) / (difference in steps)
```

Repeat it twice. If the three results disagree by more than a few per cent,
backlash or a slipping coupling is in the reading. Note it rather than
averaging it away.

## 4. Is there room for stars?

A near object focuses further **out** than a star. The extra distance for an
object at distance *d* with focal length *f* = 1000 mm:

```
Δ (mm) ≈ f² / (d − f)            1 km → 1.0 mm · 500 m → 2.0 mm · 200 m → 5.0 mm
Δ (steps) = Δ (mm) × 1000 / step_size_um
```

From P_target, how many steps of **inward** travel remain before the end stop
(0 or `FOCUS_MAX`, depending on step 1)?

- **Pass:** the inward travel left is larger than Δ, with a comfortable
  margin on top. A couple of millimetres of margin covers temperature, other
  filters and a slightly different spacer stack. That margin is a judgement,
  not a specified threshold. Record what you found and let the margin be
  decided from it.
- **Also check:** with the drawtube at the star position (P_target − Δ),
  look down the front of the tube. The drawtube must not reach into the light
  path. If it does, it vignettes every frame.

## If it does not reach

The spacers are not the fix. They set the 55 mm between the MPCC and the
sensor, and that distance does not change. What has to move is the whole
MPCC-plus-camera assembly, closer to the tube. How far short it falls decides
how. Do not buy anything before it is measured:

- **Any extension or adapter between the focuser and the MPCC** comes out
  first, or is replaced by a shorter one. Report exactly what is fitted there.
- **Moving the primary mirror up the tube** is the usual last resort on
  Newtonians. It is a hardware modification and needs its own decision.
- Report the shortfall in millimetres, not in steps. Steps mean nothing until
  the step size is known.

---

## What to record, and where it goes

Send these to the development log, which becomes field notes:

1. Spacer stack, part by part, and the filter in the beam.
2. Target and its distance.
3. Step direction (increasing = in / out), current and maximum EAF positions.
4. P_target, the three step-size measurements, and the inward travel left.
5. Pass or fail, and the drawtube check.

**Configuration.** Edit your own `config/equipment.local.yaml`, never the
shipped file, as ADR 0013 requires:

- `optical_trains` is a **list, and lists do not merge**. Restate both trains
  in full, with `mpcc` set `active: true` and `native` set `active: false`. Do
  not add `central_obstruction_mm` yet: the schema refuses it until M4 (ADR
  0017).
- `focuser.step_size_um`: the caliper value. Write down that it came from a
  caliper on this date. The M2 measurement may refine it.
- Run `nocturne check-config` and confirm both changes appear in its list of
  overrides.

**Consequences already recorded elsewhere.** The `mpcc` train invalidates any
flat library (SPEC §10.2). Its focal length must be re-derived from plate
solves once M2 can solve (SPEC §15).
