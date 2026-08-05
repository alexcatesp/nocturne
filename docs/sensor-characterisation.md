# Sensor characterisation

SPEC section 15: read noise, e⁻/ADU and full well **must be measured, not taken
from the datasheet**. The exposure solver of SPEC section 8.1 divides by these
numbers; a datasheet value that is 20 % out makes every sub exposure 20 % wrong.

> **Status: not yet measured.** `config/equipment.yaml` carries placeholder
> values for the `hcg` gain profile, marked as such. The measurement procedure
> and the analysis land with **M3**, where the exposure solver that consumes
> them is built. This file is the placeholder SPEC section 6 requires, and it
> records what has to be produced.

## What has to be measured, per gain profile

| Quantity | Unit | Used by |
|---|---|---|
| `read_noise_e` | e⁻ RMS | Optimal sub exposure (SPEC section 8.1) |
| `e_per_adu` | e⁻/ADU | Converting measured background rate to electrons |
| `full_well_e` | e⁻ | Flat exposure target, 50 % of full well (SPEC section 10.2) |

At minimum for gain 100 (`hcg`), the profile the reference rig images at.

## Outline of the procedure

The standard photon transfer method, all frames at the configured sensor
temperature (−10 °C):

1. **Bias frames.** Shortest possible exposure, shutter closed, in the dark.
   Read noise in ADU is the standard deviation of the difference of two bias
   frames divided by √2. Multiply by `e_per_adu` for electrons.
2. **Flat pairs at a range of levels.** For each level, take two flats. Signal
   is the mean of one frame minus the bias; noise is the standard deviation of
   the difference of the pair divided by √2.
3. **The photon transfer curve.** Plot variance against signal. The slope of the
   linear region is `1 / e_per_adu`.
4. **Full well.** The signal level at which the curve departs from linearity.

## Recording the result

Add or amend a profile in `config/equipment.yaml`:

```yaml
gain_profiles:
  - name: "hcg"
    gain: 100
    read_noise_e: 1.5      # measured YYYY-MM-DD
    full_well_e: 21000     # measured YYYY-MM-DD
    e_per_adu: 0.32        # measured YYYY-MM-DD
```

The schema requires all four to be present and positive; there is no default and
no fallback to a datasheet.
