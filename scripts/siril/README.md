# Siril stacking templates

The Siril CLI job templates of SPEC section 10.3 arrive with **M3**, which is
where the four stack variants (`all`, `strict`, `best_fwhm`, `best_guiding`) and
their comparative metrics are built.

Nothing here stretches, removes gradients or does colour work. SPEC section 1.2
puts all of that on the operator's PC, deliberately: the Pi produces calibrated,
registered, stacked 32-bit FITS and stops.
