# Source and license

All bundled data is original synthetic demonstration data for fictional entities.
It is not real customer information, official measurements, live service history,
or copied third-party content. It is distributed under this repository's MIT
license. No third-party source code is vendored; installed dependencies retain
their own licenses. No paid service, real account or external dataset is required.

`tiled_zones.geojson` and `tiled_parcels.geojson` are hand-constructed synthetic
fixtures for the tiled entry point: one zone `[-5,15]x[-5,5]`, the parcel `TP`
(woodland) `[-5,5]x[-5,5]` and `TQ` (grass) `[5,10]x[-5,5]`. They exist to
demonstrate negative coordinates, grid-unaligned boundaries and explicit
uncovered area; no real land data is involved.

`connectivity_zones.geojson` and `connectivity_parcels.geojson` are
hand-constructed synthetic fixtures for the connectivity entry point: one zone
`[0,3]x[0,1]` and three `forest` parcels A, B and C, each the unit square
`[k,k+1]x[0,1]`. They demonstrate the A-B-C chain whose middle parcel is the
most damaging single removal. No real land, species or ecological survey data
is represented.

`acquisition_network.json` is a hand-constructed synthetic `SurveyNetwork` for
the offline acquisition entry point. It contains 14 nodes and 15 straight edges:
three concentric squares of side 10, 6 and 2 owned by parcel `P_NEST`
(woodland, 68 m2 after the hole and island rule), and a parcel `P_EAST` (grass,
100 m2) that shares the outer square's right boundary edge. It demonstrates
survey-only topology, a hole with an independent island face and a two-parcel
shared edge. No real parcel, survey measurement, coordinate system or land
cover is represented.

`transition_zones.geojson`, `transition_parcels_before.geojson` and
`transition_parcels_after.geojson` are hand-constructed synthetic fixtures for
the two-period transition entry point: one 100 m2 zone `TZ` `[0,10]x[0,10]`;
before, parcel `B1` (forest) `[0,6]x[0,10]` with the right 40 m2 uncovered;
after, parcel `A1` (forest) `[0,4]x[0,10]` and parcel `A2` (grass)
`[4,10]x[0,10]`. They demonstrate forest->forest 40 m2, forest->grass 20 m2
and uncovered->grass 40 m2 with deliberately different parcel IDs across
periods. No real land data is involved.
