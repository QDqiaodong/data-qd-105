# Planar land-cover profile v1

Two JSON FeatureCollections use GeoJSON geometry encoding, with mandatory foreign
member coordinate_system=LOCAL_METERS. Coordinates are synthetic engineering
XY meters, NOT RFC7946 longitude/latitude. No EPSG identity, geodesic area or
reprojection is claimed. Each coordinate is finite, two-dimensional, |v|<=10^6.
Rings must have >=4 positions and explicit closure.
Only valid nonempty positive-area Polygon/MultiPolygon geometries are accepted;
self-intersection is rejected rather than silently repaired.

Zone properties exactly {zone_id}; parcel properties exactly {parcel_id,class};
IDs/classes are nonempty strings, IDs unique within each input. Zone collection
must be nonempty; empty parcel collection is allowed. Zones cannot overlap with
positive area, nor can parcels; shared boundaries are allowed. A polygon hole is
real uncovered area unless another non-overlapping parcel fills it.

One fragment is a zone/parcel intersection with positive area. GeometryCollection
line/point-only pieces are discarded because this is areal accounting. Uncovered
zone geometry becomes an explicit fragment with null parcel/class and
coverage_kind=unclassified. A real class named 'unclassified' remains classified;
coverage_kind disambiguates it. Parcels outside all zones contribute no summary.
Per-zone classified + uncovered must equal zone area within rel 1e-9 / abs 1e-8 m2.
Coverage fraction = classified/zone area; class areas are summed clipped areas,
not parcel centroid counts. GEOS floating precision is used, no rounding grid.

Outputs: fragments.geojson, coverage.json, coverage.sqlite (fragments with encoded
geometry, zone_summary, input_hashes). SQLite is not SpatiaLite and geometry is
queried as JSON text; numeric area fields are directly queryable. Full recompute
replaces named tables and reports deterministically, single writer. DB commits
before JSON files; no multi-file crash publication protocol. Input errors occur
before writing new results. Baseline uses pairwise topology checks and nested
zone/parcel overlay, without spatial indexing, tiling, mixed CRS or overlap repair.

## Tiled build mode

`python -m landcover_cube.tiled --zones Z --parcels P --tile-size S --output O`
(Python: `landcover_cube.tiled.tiled(zones_path, parcels_path, tile_size, output,
observer=None)`) consumes the same two FeatureCollections and the same validity,
identity, ring, Polygon/MultiPolygon and same-layer positive-area-overlap contract.
The whole of both inputs is validated first; parcels outside every zone are not
skipped. Empty parcels are allowed; zones must be nonempty. `tile-size` is a
finite decimal number of meters, strictly greater than 0 and at most 1000000;
booleans, NaN/Infinity and malformed text are rejected. As a practical
implementation guard (not a change to that numeric contract), a tile size that
would require more than 5,000,000 blocks for any single zone is rejected before
allocation with a message asking to increase tile-size; normal synthetic
datasets are far below this bound.

The grid is anchored at (0,0): block (i,j) is `[i*s,(i+1)*s] x [j*s,(j+1)*s]`,
with negative i/j allowed. Shared grid boundaries contribute zero area; only
positive-area polygon parts become facts. Grid indices use true floor division,
never truncation toward zero. Zones may cross blocks, contain holes or multiple
components; parcels may cross zones and blocks.

Scope reduction is real. Same-layer topology validation builds a bbox index,
computes the union of unordered candidate pairs across queries and calls exact
areal intersection at most once per pair. Only blocks whose exact zone
intersection has positive area are processed. Per block, the spatial index
returns candidate parcels; each candidate is intersected with the zone part,
and uncovered area is `zone_part - union(all candidate parcel geometries in
that block)`, so a temporarily uncovered position is never mistaken for final
empty space and no uncovered fragment is emitted from per-parcel processing
alone. Local positive-area pieces are written to a private SQLite staging table
as they are produced (not accumulated as a Python list of all blocks). After
staging, every `(zone_id, coverage_kind, parcel_id)` business key is loaded as a
group, unioned into one Polygon/MultiPolygon, and only then are final facts,
reports and SQLite rows written. Disconnected components and holes are retained;
a real class named `unclassified` stays `coverage_kind=classified` with that
class string. No snapping, simplification, buffering or area threshold is
applied; any positive-area piece is kept. Area conservation uses the baseline
rel_tol=1e-9 / abs_tol=1e-8 m2. The union of final facts is compared with each
zone by symmetric difference, and final facts are checked for positive-area
overlap, both within max(1e-8, 1e-9*zone_area_m2); equal area in the wrong
location therefore fails. Invalid input or same-layer overlap rejects the whole
run rather than being tolerated by a final area check.

Final fact granularity remains `(zone_id, parcel_id)`; unclassified remains one
row per `zone_id` with null parcel/class. `fragment_id` is a stable 1-based
order over `(zone_id, coverage_kind, parcel_id)`: zone_id ascending, then
coverage_kind (`classified` before `unclassified`), then parcel_id ascending
with null sorted first.
`fragments.geojson` keeps LOCAL_METERS; SQLite geometry stays JSON text and is
not SpatiaLite. Each GeoJSON feature also carries `fragment_id` as a property,
matching the SQLite primary key of the same fact.

The new target directory must not pre-exist and contains `fragments.geojson`,
`coverage.json`, `coverage.sqlite` and `build_diagnostics.json`. The first two
inherit the field meanings above. coverage.json adds `mode=tiled`, `tile_size`,
and `grid_anchor=[0,0]`; the baseline mode is unchanged. The three business
outputs come from the same merged facts: GeoJSON, coverage.json and every
SQLite `fragments`/`zone_summary`/`input_hashes` row. Both input files are read
once; parsing, summary hashing and later computation use that same captured
byte content, never a re-read after parsing. Diagnostics record, from the
actual execution path: per-processed-block (i,j), input feature counts, layer
candidate-pair and exact topology-check counts, per-block index candidate and
actual clip counts, staged positive-area piece counts, and final fact counts.
Definitions distinguish index hits from exact geometric operations. Diagnostics
contain no duration, wall-clock time or absolute machine path.

Diagnostic keys (all emitted from the actual run):

- `inputs.zones` / `inputs.parcels`: `features` (validated feature count),
  `candidate_pairs` (deduplicated unordered bbox-candidate pairs), and
  `exact_topology_checks` (actual GEOS intersection calls on those pairs).
- `blocks.items[]`: `i`, `j`, `zone_candidates` (index zone hits for the block),
  `zone_parts` (zones with a positive-area block intersection),
  `parcel_candidates` (index parcel hits, not exact operations),
  `classified_clips` (zone-part/parcel intersections actually executed),
  `unclassified_clips` (zone-part minus block candidate-union differences),
  and `staged_pieces` (positive-area pieces inserted into private staging).
- `totals`: sums of the above plus `final_facts` (merged business-key facts).

`blocks.processed_count` counts blocks whose zone part had positive area; blocks
touching a zone only along a grid line are not counted and emit no facts.

Determinism: identical input bytes and tile size reproduce byte-identical JSON
and GeoJSON, and logically identical SQLite business-key content (database file
bytes need not match). Changing tile size may change partitioning, diagnostics
and geometry encoding but not business keys, classes, spatial meaning or area
indicators within the above tolerances. Reordering input features changes the
source digest truthfully while preserving final business semantics.

Failure atomicity: inputs are validated before any target is published; work
happens in a private temporary directory next to the target. All files are
written and the database closed before the finished directory is published.
An existing target is refused and never overwritten. Success or failure never
modifies the input files. Errors in input, geometry, merge, database, or report
writing exit nonzero (CLI) / raise, identify the input role, feature, block or
zone where applicable, leave no partial official target, and clean this run's
temporary directory; after fixing the cause the same target can simply be
retried. Only single-writer normal/exception safety is claimed, not power-loss
recovery or concurrent publish protocols.

GEOS floating-point limits: areas come from GEOS planar 64-bit floating-point
geometry. Exact grid lines are represented as the same float values on both
sides of a boundary, so an areal part with zero width contributes exactly zero
area; nevertheless coordinates are not snapped to a decimal grid, and results
carrying genuine numerical noise are accepted only within the stated tolerances.
A negative coordinate exactly on (or a tiny distance either side of) a grid line
uses decimal-text-based floor semantics; a part whose true area is positive but
numerically ~1e-16 may be reported with a slightly different magnitude than in
the baseline, while still being retained rather than dropped.

When `tile-size` is not an exact binary fraction (e.g. 0.1 or 2.3), tile edges
are computed as the nearest float of the exact decimal product `k*s`; the same
float value is used on both sides of a shared edge, so adjacent block pieces
meet exactly and the union has no spurious overlap or gap. Coordinates near a
grid line are not snapped: a parcel corner that should lie on the line but
differs by float rounding may create a genuinely positive sliver. Because
slivers are never deleted, such a sliver is reported (usually far below 1e-8
m2) rather than silently absorbed; this is within the stated area tolerances
and is an acknowledged GEOS/float limitation, not a repair step.

