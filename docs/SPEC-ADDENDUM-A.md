# Nocturne — Specification Addendum A

## Scientific characterisation and data provenance

Version 1.0 · Extends SPEC.md · To be merged as sections §16–§18 and a revised §14

---

> ## Repository note — placement, not content
>
> **This addendum is authoritative and is not yet merged into SPEC.md's section
> numbering.** It ships as its own document until M3, for the reasons in
> [ADR 0016](decisions/0016-addendum-a-placement.md). Everything below §A.0 is
> the operator's text, unedited. This block is the only editorial addition.
>
> Two things have moved since it was written:
>
> - **§A.8.4 reserves `time_source` including `gps_pps` "for future occultation
>   work". GPS and PPS are now fitted and verified** on the reference rig —
>   chrony selects PPS as its reference clock with an estimated error of ±155 ns
>   against the ≤100 ms this addendum asks for. See
>   [`FIELD-NOTES-TIMING.md`](FIELD-NOTES-TIMING.md). The reservation stands as
>   written; it is simply no longer hypothetical, and the schema should be built
>   knowing the hardware exists.
> - **§A.3's photometric sky brightness supersedes an SQM device**, and that
>   decision now has to hold against a lux sensor as well. A VEML7700 was
>   prototyped on the same I²C bus for an unrelated project. It is not Nocturne
>   hardware and must not become a second path to this measurement — see
>   §0 of the timing field notes.
>
> Neither changes a requirement here. Both change what is already possible.

---

## A.0 Why this addendum exists

SPEC.md v1.0 specifies a system that produces *pleasing* images. This addendum
specifies a system that produces *publishable* measurements. Those are different
requirements, and the difference is not a set of extra features bolted on at the end —
it propagates backwards through calibration, telemetry, storage and frame selection.

Merge this before M3. Retrofitting provenance onto data already collected is not
possible; the data is simply unusable for the purpose.

**Nothing in this addendum modifies §9 (safety governor).** The characterisation tools
are read-and-measure only. They must not acquire authority to command the mount beyond
what §8.2 already grants, and any new tool they introduce passes through
`safety.validate()` like everything else.

### A.0.1 The governing principle

> A number without an uncertainty is not a measurement. A measurement without
> provenance is not evidence.

Every quantity this system reports as scientific output carries: its uncertainty, the
method by which it was derived, the calibration data it depended on, and the software
version that computed it. If any of those cannot be supplied, the quantity is reported
as an engineering diagnostic and explicitly flagged as not science-grade.

---

## A.1 Prerequisite: sensor characterisation (promoted from §15)

Sensor characterisation ceases to be an open item and becomes a **hard prerequisite for
M3**. Photometry without measured linearity is not photometry.

### A.1.1 Required measurements

| Quantity | Method | Output |
|---|---|---|
| Gain (e⁻/ADU) | Photon transfer curve — flat pairs at increasing illumination | Per gain setting, with σ |
| Read noise (e⁻) | Bias frame pair differencing | Per gain setting, with σ |
| Full well / linearity | PTC extended to saturation | Linear range in ADU, and the deviation curve |
| Dark current | Dark series vs. temperature and exposure | e⁻/px/s at each setpoint |
| Bias stability | Repeated bias series over a session | Drift in ADU |

Implement as `nocturne.characterisation.sensor`, driven by a CLI command
(`nocturne characterise-sensor`) that walks the operator through the acquisition and
produces `docs/sensor-characterisation.md` plus a machine-readable
`config/sensor-profile.yaml` consumed by the photometry pipeline.

### A.1.2 The linearity requirement

The linear range determines the maximum usable ADU for photometry, which is generally
**well below** saturation. The pipeline must:

- Store `linear_max_adu` per gain setting in `sensor-profile.yaml`
- Flag any measured star whose peak exceeds it as non-photometric
- Reject frames where the target star exceeds it, distinctly from the §7.3 verdicts

### A.1.3 Acceptance

Gain and read noise agree with ZWO's published figures within 15 %, or the discrepancy
is investigated and documented. Linearity measured across at least 12 illumination
levels. Dark current characterised at every cooling setpoint the system will use.

---

## A.2 §16 — Photometric calibration

This is the foundation. Sky brightness, transparency and extinction all derive from
one quantity: **the per-frame photometric zero point.**

### A.2.1 Zero point derivation

Per frame, after a successful plate solve:

1. Detect and centroid sources (`photutils`), rejecting those flagged non-linear,
   saturated, blended, or within a configurable distance of the frame edge
2. Aperture photometry with local background annulus; aperture radius derived from the
   measured FWHM of the frame, not fixed
3. Cross-match against **Gaia DR3** using the WCS solution, within a configurable radius
4. Use Gaia synthetic photometry in the closest available band to each filter
5. Fit the zero point as the sigma-clipped weighted mean of
   `ZP = m_catalogue − m_instrumental`, reporting the fitted value, its standard error,
   the number of stars used, and the number rejected

**Catalogue access:** the system operates offline by design (§4). Gaia cross-match
requires a locally cached catalogue subset. The installer must fetch a magnitude-limited
Gaia DR3 subset covering the observable sky for the site, or the system must degrade
explicitly: without the catalogue, photometric outputs are unavailable and the tools
report that, rather than silently falling back to an uncalibrated estimate.

### A.2.2 Filter transformation

**The operator's L/R/G/B filters are imaging filters, not a photometric system.**
Reporting magnitudes in them without qualification is incorrect.

The system must:

- Store, per filter, a `photometric_system` field: `none` | `johnson_cousins` | `sloan`
- For `none`, derive and apply colour transformation coefficients from field stars,
  reporting the fit residual as an additional uncertainty term, and label all outputs as
  transformed instrumental magnitudes with the transformation stated
- For a recognised system, apply the standard transformation and report the band

**Scope of this limitation.** It applies to *absolute* photometry only. Differential
measurements — the operator's primary science use, per §A.7 — are unaffected, because
the filter response cancels in the ratio. LRGB is a fully adequate filter set for
transit and eclipsing-binary work.

**Recommendation to the operator, recorded here as a design assumption:** the EFW has
three free positions. Fitting Sloan g′r′i′ (or Johnson-Cousins V and R) would extend the
system from differential to absolute photometry and remove an irreducible uncertainty
term from magnitudes reported in a standard system. It is not a prerequisite for the
science described in §A.11, and the operator has deferred it. Should it be adopted, it
should be done *before* accumulating an absolute-photometry archive, since data in
non-standard filters cannot be retroactively standardised to the same quality.

### A.2.3 Outputs

Per frame: `zero_point`, `zero_point_err`, `n_stars_fit`, `n_stars_rejected`,
`transformation_applied`, `transformation_residual`, `catalogue_version`.

---

## A.3 §16.2 — Sky brightness

Derived from the zero point and the measured background level:

```
μ_sky [mag/arcsec²] = ZP − 2.5 · log10( B_background [ADU/s/px] / pixel_scale² )
```

with pixel scale from the plate solve, not from configuration.

Requirements:

- Reported **per filter**. A single "sky brightness" number is meaningless for a mono
  system with multiple bands.
- Uncertainty propagated from `zero_point_err` and background estimation error
- Recorded with altitude, azimuth, moon phase, moon separation, moon altitude, and time
  from twilight — a value without these is not interpretable
- Aggregated per session and stored in a long-lived series, since the multi-year record
  of a single site is itself a publishable dataset

This measurement supersedes the need for an SQM device and is strictly better: it is
per-band, per-pointing, and timestamped against every frame.

---

## A.4 §16.3 — Transparency and atmospheric extinction

### A.4.1 Transparency

The zero point tracked over time *is* transparency. A frame's transparency index is its
zero point relative to the best zero point recorded for that filter and optical train
under photometric conditions.

The `transparency_index` field in §7.2 is redefined: it ceases to be a heuristic and
becomes this ratio. Where no photometric reference exists yet, the field reports `null`
rather than a guess.

### A.4.2 Extinction coefficient

Zero point plotted against airmass over a night yields the **Bouguer line**; its slope
is the extinction coefficient *k* for that filter and night.

Implement as a session-level analysis:

- Requires frames spanning a useful airmass range (configurable minimum, default 0.4)
- Linear fit with outlier rejection; report *k*, its standard error, the airmass range
  covered, the number of points, and the fit residual
- Detect and flag non-photometric nights: a poor fit means variable transparency, which
  is itself the finding
- Store per night, per filter, building a site extinction record

**This is a publishable measurement in its own right**, and it is a free by-product of
imaging that already spans altitude.

---

## A.5 §16.4 — Seeing

### A.5.1 The correctness requirement

**Measured FWHM is not seeing.** It is the convolution of atmospheric seeing,
instrumental PSF, focus error, tracking error and pixel sampling. Reporting FWHM as
seeing is a defect, not a simplification, and this specification requires the
distinction be maintained in the field names themselves.

### A.5.2 Implementation

The system reports two distinct quantities:

| Field | Meaning |
|---|---|
| `fwhm_measured_arcsec` | The raw measurement. Always available. An engineering diagnostic. |
| `seeing_estimate_arcsec` | Deconvolved atmospheric contribution. Available only when the instrumental PSF is characterised. Always accompanied by its uncertainty. |

Deconvolution subtracts the instrumental contribution in quadrature:

```
FWHM_seeing² ≈ FWHM_measured² − FWHM_instrument² − FWHM_tracking²
```

where `FWHM_instrument` is derived from the optical characterisation of §A.6 and
`FWHM_tracking` from the guiding RMS over the exposure.

### A.5.3 The sampling caveat — must be documented in output

At 0.776 ″/px, a 2″ seeing disc spans ~2.6 px FWHM. This is marginally above Nyquist.
The measurement is usable but its uncertainty is significant and grows as seeing
improves. The system must:

- Compute and report the sampling-limited uncertainty floor
- Refuse to report `seeing_estimate_arcsec` below a configurable sampling limit,
  reporting an upper bound instead
- State the sampling in any exported data product

Undersampling is a property of the instrument, not a bug to hide. Report it.

---

## A.6 §16.5 — Optical evaluation

The most immediately useful of the four, and the least methodologically fraught.

### A.6.1 Field analysis

Per frame, divide the field into a configurable grid (default 5×5) and report per cell:
FWHM, eccentricity, and eccentricity position angle, each with its scatter and star
count. Derived products:

- **Tilt**: a linear gradient in FWHM across the field
- **Field curvature**: radially symmetric FWHM variation
- **Coma residual**: eccentricity increasing radially with position angles pointing
  radially — the signature to watch when commissioning the MPCC
- **Collimation error**: the FWHM minimum displaced from the field centre

### A.6.2 Flexure test

An operator-initiated procedure, not part of a normal session:

1. Capture a short series at a sequence of altitudes (default 30°, 45°, 60°, 75°, 85°)
   on fields of comparable star density
2. Compute the field analysis of §A.6.1 at each
3. Report whether the tilt vector **rotates with altitude** (gravitational flexure,
   mechanical remedy) or **remains fixed** (static tilt, correctable with spacers)

This distinction determines whether the remedy is a focuser upgrade or a spacing
adjustment, and the two are routinely confused. The procedure must run under the same
safety constraints as any other slewing operation.

### A.6.3 Instrumental PSF characterisation

Feeds §A.5. Under the best seeing recorded, with confirmed focus, the measured FWHM in
the field centre bounds the instrumental PSF from above. The system maintains a rolling
best-ever value per optical train as the working estimate, flagged as an upper bound
rather than a measurement.

### A.6.4 Optical train invalidation

Any change to `optical_train_id` invalidates: the instrumental PSF estimate, the flat
library (already specified in §10.2), the tilt baseline, and the derived focal length.
The system must detect the change and mark all four as requiring re-derivation, refusing
to report science-grade products until they are.

---

## A.7 §16.6 — Photometry session mode

### A.7.1 Why this is a separate mode, not a set of options

This is the most important section of this addendum, because it identifies a conflict
that SPEC.md v1.0 does not merely omit — it actively specifies the wrong behaviour.

Several defaults that are correct for deep-sky imaging are **destructive** for a light
curve:

| SPEC.md default | Why it is correct for deep-sky | Why it ruins photometry |
|---|---|---|
| Dither every frame (§5.1) | Averages out pixel-level defects and walking noise | Moves the star across pixels of differing response, injecting noise into exactly the signal being measured |
| Autofocus on ΔT / HFD drift (§5.1) | Keeps stars tight as the tube cools | A refocus mid-transit puts a step discontinuity in the curve at millimagnitude scale |
| Meridian flip (§9.1) | Extends the usable night | Produces a systematic offset mid-event |
| Focus for minimum HFD | Maximum detail and depth | Concentrates flux into few pixels, risking non-linearity and maximising sensitivity to flat-field error |
| Exposure maximising sky-limited SNR (§8.1) | Correct for faint extended targets | Wrong constraint entirely — the binding limit is keeping the target within the linear range |

A photometry session is therefore a distinct mode with its own defaults, its own
validation, and its own planner. It is **not** the deep-sky mode with dithering switched
off.

### A.7.2 Differential photometry is the primary science mode

SPEC Addendum A §A.2 specifies absolute calibration. That remains valuable for sky
brightness and extinction. But the operator's actual science targets — eclipsing binary
minima and exoplanet transits — are **differential** measurements: the target is measured
against comparison stars in the same frame, through the same filter, at the same airmass,
in the same atmosphere.

Everything that is not standardised cancels. **This means the operator's LRGB filters are
not a limitation for this work.** Absolute standardisation is unnecessary when the
measurement is a ratio.

Reporting follows the convention for unstandardised filters (AAVSO band codes `TG`, `TB`,
`TR` for tricolour filters), which is a recognised and reportable category, not a
compromise to be hidden.

**Filter selection for transit work:** the R filter is preferred, and this is a positive
recommendation rather than a fallback. Limb darkening is weaker at longer wavelengths,
giving a flatter transit floor that is easier to fit; differential extinction between
target and comparison stars is reduced; and scintillation contributes less. The L filter
must **not** be used for transits — its bandwidth maximises colour-dependent differential
extinction, injecting spurious slopes into the curve precisely as airmass changes.

### A.7.3 Mode parameters

```yaml
photometry_session:
  dithering: false                    # hard override; not a tunable in this mode
  refocus_during_series: false
  guiding_aggressiveness: "high"      # keep the target on the same pixels
  max_centroid_drift_px: 5            # abort or re-centre beyond this

  defocus:
    enabled: true
    method: "focuser_offset"
    offset_steps: null                # calibrated per target brightness
    target_peak_adu_fraction: 0.5     # of linear_max_adu, not of saturation
    stability_tolerance_steps: 5      # defocus must be *stable*, not merely present

  exposure:
    constraint: "linearity"           # not "sky_limited"
    target_peak_fraction: 0.5
    max_exposure_s: 300
    duty_cycle_min: 0.85              # minimise dead time between frames

  window:
    baseline_before_minutes: null     # default: one event duration
    baseline_after_minutes: null      # default: one event duration
    require_no_meridian_crossing: true
    min_altitude_deg: 30              # stricter than the deep-sky floor

  comparison_stars:
    min_count: 3
    max_magnitude_difference: 1.5
    max_colour_difference: 0.5
    check_variability: true           # against catalogue flags
```

### A.7.4 Behaviour

**Defocus must be stable, not merely applied.** A drifting defocus is worse than sharp
focus. The system monitors focuser position and measured FWHM through the series and
raises `DEFOCUS_DRIFT` if either moves beyond tolerance. Temperature compensation, if
enabled elsewhere, is **disabled** during a photometry series.

**Baseline is mandatory.** Out-of-transit data on both sides is what makes the fit
possible. A session that starts late or ends early produces a curve that cannot be
normalised. The planner refuses targets whose event window plus baselines does not fit
within the altitude and meridian constraints, and reports why.

**Meridian crossing is avoided by planning, not handled by flipping.** Where
unavoidable, the crossing is recorded explicitly in the output so the systematic offset
can be modelled downstream rather than silently absorbed.

**Guiding is not dithered and not paused.** Loss of guiding during a series is a more
serious event than in deep-sky mode: it moves the star. Threshold for
`GUIDING_DEGRADED` is tightened in this mode.

### A.7.5 Outputs

Beyond the standard frame records:

- **Differential light curve**: target instrumental magnitude minus the weighted
  ensemble of comparison stars, per frame, with propagated uncertainty
- **Check star curve**: an unused comparison star processed identically, as a control.
  A flat check star validates the measurement; a varying one invalidates it. This is
  not optional.
- **Airmass, sky background, FWHM and centroid position** plotted alongside the curve —
  a light curve correlating with any of these is a systematic, not a detection
- **Export formats**: AAVSO Extended File Format for variable star work; ExoClock and
  ETD-compatible output for transits. FITS remains the archival product.

The system computes and reports the **photometric scatter of the check star** as the
headline quality metric for a photometry session, in place of the SNR-based metrics used
in deep-sky mode.

### A.7.6 What the agent decides here

Judgement calls specific to this mode:

- Target selection from an ephemeris of upcoming events, weighted by altitude at
  mid-event, event depth versus achievable precision, and baseline feasibility
- Comparison star ensemble selection and, if a comparison proves variable mid-session,
  its removal
- Whether a degrading night is still worth continuing: for a transit, a partial curve
  with good baseline may be worth more than an aborted one, and a curve without baseline
  is worth nothing at all. That trade-off is judgement, not threshold.

The agent must **not** be permitted to alter defocus, dithering or refocus settings
mid-series. Those are frozen at session start by the deterministic layer, and the
freezing is enforced, not conventional.

---

## A.8 §17 — Data provenance

### A.8.1 FITS headers

Every saved frame carries sufficient metadata to reconstruct its calibration
independently. Beyond the standard keywords:

**Astrometry**: full WCS solution, solver version, index files used, solve residual
**Photometry**: `ZEROPT`, `ZEROPTER`, `NZPSTARS`, `CATALOG` (with version), `EXTINCT`,
`AIRMASS`, `PHOTSYS`, `TRANSCOF`, `TRANSRES`
**Conditions**: `SKYMAG`, `SKYMAGER`, `FWHMMEAS`, `SEEING`, `SEEINGER`, `TRANSPAR`,
`MOONSEP`, `MOONALT`, `MOONILL`, ambient temperature, humidity, dew point
**Instrument**: optical train id, derived focal length, filter, gain, offset, sensor
temperature, focuser position and temperature, guiding RMS over the exposure
**Calibration provenance**: checksums and identifiers of the master dark, flat and
dark-flat applied, with their acquisition dates
**Software**: Nocturne version and git commit, INDI version, KStars version, Siril
version, sensor profile version
**Timing**: exposure start in UTC with the time source and its estimated accuracy

A frame missing any of these is not deleted — it is flagged `SCIGRADE = F` with the
reason recorded.

### A.8.2 Selection function

Frame rejection (§7.3) becomes a documented **selection function**. For any data
product, the system must be able to answer: how many frames were acquired, how many
rejected, under which criterion, with which threshold values, and what the distribution
of the rejection metric looked like.

Requirements:

- Thresholds versioned; a threshold change is a new selection function, not an edit
- Rejected frames retained with full metadata (already specified — now mandatory)
- Every stack carries a manifest of contributing frames and the selection applied
- The stacking variants of §10.3 are, in this framing, four different selection
  functions — report them as such

### A.8.3 Uncertainties

Every field in the §7.2 telemetry record that represents a measurement gains a
corresponding uncertainty field. Where an uncertainty cannot be estimated, the field is
`null` and the value is flagged as an engineering diagnostic, never as a measurement.

### A.8.4 Timing

NTP over the wired Ethernet link gives millisecond-level accuracy, sufficient for
transit and variable-star work. The system must:

- Record the time source and its measured offset in every frame header
- Refuse science-grade flagging if NTP is unsynchronised
- Reserve, in the schema, a `time_source` enum including `gps_pps` for future
  occultation work

**Decide this now**: adding GPS/PPS later is a hardware change but the schema must
accommodate it from the start.

---

## A.9 §18 — Configuration additions

New file `config/science.yaml`:

```yaml
photometry:
  enabled: true
  catalogue: "gaia_dr3"
  catalogue_path: "/var/lib/nocturne/catalogues/gaia_dr3_subset"
  match_radius_arcsec: 2.0
  min_stars_for_zeropoint: 20
  sigma_clip: 3.0
  edge_exclusion_px: 50

filters:
  L: { photometric_system: "none", transform_to: null }
  R: { photometric_system: "none", transform_to: null }
  G: { photometric_system: "none", transform_to: null }
  B: { photometric_system: "none", transform_to: null }
  # When Sloan filters are fitted:
  # "g'": { photometric_system: "sloan", band: "g" }

seeing:
  report_estimate: true
  sampling_limit_arcsec: 1.6      # below this, report upper bound only
  instrument_psf_source: "rolling_best"

extinction:
  min_airmass_range: 0.4
  min_points: 8
  max_fit_residual_mag: 0.05

optical_evaluation:
  grid: 5
  min_stars_per_cell: 10
  flexure_test_altitudes_deg: [30, 45, 60, 75, 85]

provenance:
  require_ntp_for_science_grade: true
  max_time_offset_ms: 100
```

---

## A.10 Revised milestones

Insert between M3 and M4:

### M3.5 — Scientific characterisation

**Prerequisites**: sensor characterisation complete (§A.1); Gaia subset cached.

- **AUTO**: zero point recovered within 0.02 mag on synthetic fields with injected
  stars of known magnitude. Sky brightness correct on synthetic frames of known
  background. Extinction fit recovers an injected *k* within its stated error across a
  simulated airmass range. Seeing deconvolution recovers injected atmospheric FWHM given
  a known instrumental PSF, and correctly refuses below the sampling limit. Field
  analysis detects injected tilt, curvature and coma. FITS headers complete and
  round-trip. Selection function reconstructible from the manifest for any stack.
- **HITL**: zero points from real frames stable within their stated errors across a
  photometric night. Extinction coefficient physically plausible for the site.
  Flexure test distinguishes rotating from fixed tilt on the real rig.

### M3.6 — Photometry session mode

**Prerequisites**: M3.5 complete. Linear range measured (§A.1.2).

- **AUTO**: the photometry planner refuses targets whose event window plus baselines
  violates altitude or meridian constraints, and states the reason. Dithering, refocus
  and temperature compensation are provably disabled for the duration of a photometry
  series, and the agent cannot re-enable them mid-series — asserted through the same
  entry-point exhaustion used for the safety suite. Differential curve recovers an
  injected transit of known depth and duration from synthetic frames within its stated
  uncertainty. Check star scatter reported. Defocus drift detection triggers correctly.
  AAVSO and ExoClock exports validate against their format specifications.
- **HITL**: a real transit or eclipsing binary minimum observed end to end, with
  baseline on both sides, producing a curve whose check star is flat within the reported
  scatter. This is the acceptance test for the entire scientific capability.

Milestone M4 (web application) gains a **Science** view: sky brightness and extinction
history, zero point trends, field analysis maps, the flexure test report, and live
differential light curves during a photometry session with the check star displayed
alongside.

---

## A.11 Scope statement

This system is a 200 mm f/5 reflector at a Bortle 5–6 residential site. It is not
competitive for deep imaging against dark-site or remote-observatory data. It is
competitive, given rigorous calibration and consistent cadence, for:

- Variable star photometry (AAVSO)
- Exoplanet transit timing (ExoClock, TESS follow-up)
- Asteroid light curves and rotation periods
- Nova and supernova follow-up photometry
- **Long-term site characterisation** — sky brightness and extinction series from a
  single location, which is publishable in its own right and arrives as a by-product

Automation is the advantage here, not aperture. Cadence, consistency and calibration
discipline are what a machine does better than a person at 3 a.m.

---

## A.12 Open items introduced by this addendum

- Gaia DR3 subset: size, magnitude limit, and sky coverage for the site must be
  determined. This may be tens of GB and affects the storage specification.
- Photometric filter procurement: a decision for the operator, with the strong
  recommendation of §A.2.2. Blocks nothing, but delaying it devalues data collected
  meanwhile.
- Aperture photometry vs. PSF photometry: apertures are specified here as the simpler
  and more robust choice for uncrowded fields. Crowded fields require PSF fitting.
  Defer, but do not preclude in the schema.
- Ephemeris sources for transits and eclipsing binaries: which catalogues, how often
  refreshed, and how they are cached for an offline system. Blocks the M3.6 planner.
- Defocus calibration: the mapping from target brightness to focuser offset must be
  derived empirically per optical train. Procedure not yet specified.
- Scintillation: at 200 mm aperture it is often the dominant noise source for bright
  transit hosts. The system should estimate it (Young's approximation from aperture,
  airmass and exposure) and report it as a separate term in the error budget, so that a
  night limited by scintillation is recognised rather than mistaken for poor technique.
