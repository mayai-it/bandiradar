"""Intelligence track — ANAC historical analytics, SEPARATE from the matcher.

This package ingests ANAC historical OCDS data (awarded public contracts) and
computes compact benchmarks per (CPV-division x region). It is deliberately
independent of the Source/Opportunity/matcher pipeline: different data (awarded,
retrospective), different store, different CLI surface.

Honest data caveats (the OCDS award data drives what we CAN compute):
- It is RETROSPECTIVE: awarded contracts (> EUR 40k), not open calls.
- It has awards + suppliers but NO tenderers list, so we CANNOT derive a
  "number of bidders". We derive award VALUE distribution, VOLUME, SEASONALITY
  (per year) and SUPPLIER counts.
- The OCP/ANAC release addresses carry city + postal code but NO region/NUTS, so
  benchmarks are national-only for now (region stays None); the model and
  aggregation already support regional buckets for when a region-bearing source
  is added.

Source: ANAC, via the Open Contracting Data mirror (CC BY 4.0, no auth).
"""
