# Meridian calibration

**Read this before enabling any unattended mode.** SPEC section 9.1.

Your 200PDS has no tripod extension fitted. Near the meridian the tube — or the
camera, filter wheel and focuser hanging off the back of it — will reach a
tripod leg. Nothing in software knows where that happens on *your* tripod, with
*your* imaging train, at *your* latitude. You have to measure it.

Until you have, Nocturne ships with:

```yaml
limits:
  meridian:
    calibrated: false
```

and **refuses to point the telescope at all** until you have done this. Not
just unattended: every slew, in every mode, is rejected with a message pointing
back at this page (ADR 0019). Planning, cooling, filters, focus and parking all
work — anything that moves the tube does not.

That is deliberate. With no measured limits there is no number to enforce, and
a rule invented to fill the gap would read like protection while being a guess
about your tripod. So the software refuses rather than guesses, and this
procedure is the thing that turns it on.

---

## What you are measuring

The **hour angle** at which clearance runs out, on each side of the meridian.

Hour angle is how far the mount has driven past the meridian, in degrees:

- **Negative** — the target is east of the meridian, rising. The tube is on the
  west side of the mount.
- **Zero** — the target is on the meridian.
- **Positive** — the target is west of the meridian, setting.

A hard limit at hour angle −20° means: stop, or flip, twenty degrees (eighty
minutes of tracking) before the meridian.

You will end up with two numbers, and Nocturne narrows both by
`safety_margin_deg` before enforcing them.

---

## Before you start

- Daylight. You need to see the gap, and you are not imaging.
- The rig in its **normal imaging configuration**: OTA, camera, filter wheel,
  focuser, guide scope, dew shield, every cable dressed as you actually dress
  it. The camera corner is usually what hits first, not the tube.
- Counterweight fitted as you fit it. `equipment.yaml` records
  `counterweight_fitted`, and **changing it invalidates this calibration**
  (SPEC section 9.1.7).
- Mount powered, polar aligned roughly, **tracking off**.
- A hand. Clearance is "a hand's width", about 10 cm. If you want a number,
  use 10 cm.
- Someone to watch the gap while you drive, if you can get them. This is easier
  with two people.

> Move the mount at the slowest slew rate you have, and keep a hand on the power
> switch. You are deliberately driving the tube towards an obstruction.

---

## Procedure

Do this for the **declination extremes you actually image at**. For a terrace at
41.6° N with a restricted horizon that is typically about +80° down to about
−10°; use your own range, and include the worst case.

For each declination:

1. Point the mount at that declination with the target **east of the meridian**
   (tube on the west side of the mount).
2. Turn tracking off.
3. Slew slowly in RA towards the meridian, watching the gap between the imaging
   train and the nearest tripod leg.
4. **Stop when the clearance falls to a hand's width.** Do not go further to see
   what happens.
5. Read the hour angle. In Ekos: Mount tab, or the mount's own hand controller.
   If you only have RA and local sidereal time, hour angle in degrees is
   `(LST − RA) × 15`, taken to the range −180 to +180.
6. Write it down as the **east limit** for this declination. It will be a
   negative number.
7. Now drive past the meridian — flip the mount if it has not flipped itself —
   so the target is **west of the meridian** and the tube is on the east side.
8. Repeat steps 3 to 6 going away from the meridian, and record the **west
   limit**. It will be a positive number.

Repeat for each declination in your range.

### What to record

| Declination | East limit (negative) | West limit (positive) |
|---|---|---|
| +80° | | |
| +45° | | |
| 0° | | |
| −10° | | |

**Take the most restrictive of each column**: the east limit closest to zero,
and the west limit closest to zero. Those two numbers govern every declination,
because the software does not track clearance per declination — one pair of
limits, applied always. That is deliberate; a limit that varies with declination
is a limit that can be got wrong.

---

## Recording the result

Your measurements go in `config/safety.local.yaml` — **not** in
`config/safety.yaml`. The shipped file is the one git manages; an edit there
collides with every update and can be pushed to a public repository. The local
file is untracked, survives every pull, and never leaves this machine
(ADR 0013). Copy `config/safety.local.yaml.example` if you do not have one yet.

You only write the keys you are changing; everything else comes from the shipped
file underneath:

```yaml
limits:
  meridian:
    calibrated: true
    calibration_date: 2026-08-12        # the day you measured, not today
    hour_angle_east_limit_deg: -22.0    # your most restrictive east limit
    hour_angle_west_limit_deg: 18.0     # your most restrictive west limit
    safety_margin_deg: 5                # subtracted from both; raise it if unsure
```

`calibrated: true` with either limit missing is refused at startup, loudly: the
four keys are all-or-nothing on purpose.

Then check it loaded:

```bash
.venv/bin/nocturne check-config
```

The report's last line should read:

```
Meridian:      calibrated 2026-08-12, enforced hour angle -17.0 deg to +13.0 deg
```

Those enforced numbers are your measurements minus the margin. They are what the
safety governor uses; the raw measurements are never enforced directly
(SPEC section 9.1.4).

The configuration will be **refused** if:

- `calibrated: true` with any of the three measured fields left null;
- `calibrated: false` with measured values still present — clearing the flag
  invalidates the measurement, so clear the values too;
- the east limit is not negative or the west limit is not positive;
- `safety_margin_deg` is as large as either measurement, which would invert the
  usable range.

---

## When to do it again

- The imaging train changes: coma corrector, OAG, a different camera, a longer
  dew shield.
- The counterweight is fitted or removed. Nocturne treats
  `counterweight_fitted` as invalidating (SPEC section 9.1.7).
- A different tripod, or a tripod extension. **If you fit an extension, redo
  this — the limits get wider, and stale narrow limits cost imaging time rather
  than equipment, but stale wide limits cost equipment.**
- Anything at all changes about how the rig is assembled and you are not certain.

It takes twenty minutes. The alternative is a 200PDS driving into a tripod leg
at three in the morning while nobody is watching.
